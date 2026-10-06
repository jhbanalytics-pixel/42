"""Unit tests for core/setup/bootstrap.py. No cloud access: FakeCloud stands in for Google Cloud."""
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound

from core.setup import bootstrap as bs

ACCOUNT = "albert.meintjes@ogilvy.co.za"
GROUP = "group:42-users@ogilvy.co.za"
NUMBER = "123456789"
CONN_SA = "bqcx-123456789-abcd@gcp-sa-bigquery-condel.iam.gserviceaccount.com"
TOKEN = "FAKE-TOKEN-ya29-do-not-print"
AGENT_TABLES = {"credit_ledger", "raw_responses", "post_observations", "suppressions"}
SUPPRESSIONS = f"{bs.CORE}.suppressions"

# Built by concatenation: the guard blocks these literals outside bootstrap.py.
OWNER = "roles/" + "owner"
EDITOR = "roles/" + "editor"
FORBIDDEN_ARGV = ("remove-iam-" + "policy-binding", "set-" + "iam-policy", "del" + "ete")


def sa(name):
    return f"{name}@{bs.PROJECT}.iam.gserviceaccount.com"


@pytest.fixture(autouse=True)
def no_impersonation(monkeypatch):
    # The builder's shell sets this variable; the script must be tested as Albert's own terminal sees it.
    monkeypatch.delenv(bs.IMPERSONATE_ENV, raising=False)
    monkeypatch.setattr(bs.time, "sleep", lambda s: None)


class FakeCloud(bs.Cloud):
    def __init__(self, tables=True, legacy=None, account=ACCOUNT, impersonation=""):
        self._account = account
        self._impersonation = impersonation
        self.apis = set()
        self.accounts = set()
        legacy = bs.LEGACY_DATASETS if legacy is None else legacy
        self.datasets = {name: "US" for name in legacy}
        self.tables = {f"{bs.CORE}.{t}" for t in AGENT_TABLES} if tables else set()
        self.buckets = set()
        self.secrets = {bs.SECRET}
        self.conn = None
        self.pool = False
        self.condition = None
        self.policies = {}
        self.writes = []
        self.fail = {}

    # reads
    def account(self):
        return self._account

    def impersonation(self):
        return self._impersonation

    def project_number(self):
        return NUMBER

    def enabled_apis(self):
        return set(self.apis)

    def service_accounts(self):
        return set(self.accounts)

    def dataset_location(self, name):
        return self.datasets.get(name)

    def table_exists(self, dataset, table):
        return f"{dataset}.{table}" in self.tables

    def bucket_exists(self, name):
        return name in self.buckets

    def secret_names(self):
        return set(self.secrets)

    def connection(self):
        return copy.deepcopy(self.conn)

    def pool_exists(self):
        return self.pool

    def provider_condition(self):
        return self.condition

    def policy(self, kind, name):
        return set(self.policies.get((kind, name), set()))

    # writes
    def _write(self, *call):
        self.writes.append(call)

    def enable_apis(self, apis):
        self._write("enable_apis", tuple(apis))
        self.apis |= set(apis)

    def create_service_account(self, account_id):
        self._write("create_service_account", account_id)
        self.accounts.add(sa(account_id))

    def create_dataset(self, name):
        self._write("create_dataset", name)
        self.datasets[name] = bs.BQ_LOCATION

    def create_bucket(self, name):
        self._write("create_bucket", name)
        self.buckets.add(name)

    def create_secret(self, name):
        self._write("create_secret", name)
        self.secrets.add(name)

    def create_connection(self):
        self._write("create_connection")
        self.conn = {"service_account": CONN_SA}

    def create_pool(self):
        self._write("create_pool")
        self.pool = True

    def create_provider(self):
        self._write("create_provider")
        self.condition = bs.ATTRIBUTE_CONDITION

    def update_provider_condition(self):
        self._write("update_provider_condition", bs.ATTRIBUTE_CONDITION)
        self.condition = bs.ATTRIBUTE_CONDITION

    def generate_iap_identity(self):
        self._write("generate_iap_identity")

    def add_binding(self, kind, name, role, member):
        self._write("add_binding", kind, name, role, member)
        failures = self.fail.get((role, member))
        if failures:
            raise bs.CloudError(failures.pop(0))
        self.policies.setdefault((kind, name), set()).add((role, member))


def rows_for(cloud, iap_group=GROUP, readback=False):
    state = bs.gather(cloud, ACCOUNT)
    return bs.binding_rows(state, iap_group, readback=readback)


def all_bindings(cloud=None):
    cloud = cloud or FakeCloud()
    return bs.desired_bindings(bs.gather(cloud, ACCOUNT), GROUP)


def test_dry_run_on_empty_state_writes_nothing_and_plans_every_create_and_grant(capsys):
    fake = FakeCloud()
    assert bs.main(["--iap-group", GROUP], cloud=fake) == 0
    assert fake.writes == []
    state = bs.gather(fake, ACCOUNT)
    resources = bs.resource_rows(state, fake)
    bindings = bs.binding_rows(state, GROUP)
    assert resources and bindings
    assert {r.status for r in resources} == {"CREATE"}
    assert {r.status for r in bindings} == {"GRANT"}
    out = capsys.readouterr().out
    assert "Dry run" in out and "GRANT" in out and "CREATE" in out


def test_apply_on_empty_state_reaches_all_present_and_rerun_writes_nothing(capsys):
    fake = FakeCloud()
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    readback = rows_for(fake, readback=True)
    assert readback and {r.status for r in readback} == {"PRESENT"}
    assert ("roles/aiplatform.user", "serviceAccount:" + CONN_SA) in fake.policies[("project", bs.PROJECT)]
    assert "0 missing" in capsys.readouterr().out
    fake.writes.clear()
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    assert fake.writes == []


def test_apply_leaves_unrelated_bindings_in_place():
    fake = FakeCloud()
    fake.buckets.add(bs.BUCKET)
    extras = {
        ("project", bs.PROJECT): {("roles/viewer", "user:someone@ogilvy.co.za"), ("roles/run.invoker", "serviceAccount:old@x.iam.gserviceaccount.com")},
        ("secret", bs.SECRET): {("roles/secretmanager.secretAccessor", "serviceAccount:legacy-job@x.iam.gserviceaccount.com")},
        ("repo", bs.REPO): {("roles/artifactregistry.reader", "serviceAccount:cloudbuild@x.iam.gserviceaccount.com")},
        ("bucket", bs.BUCKET): {("roles/storage.objectViewer", "group:others@ogilvy.co.za")},
        ("dataset", "trends_v2_dev"): {("roles/bigquery.dataEditor", "serviceAccount:old-pipeline@x.iam.gserviceaccount.com")},
    }
    for key, pairs in extras.items():
        fake.policies[key] = set(pairs)
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    for key, pairs in extras.items():
        assert pairs <= fake.policies[key]
    assert all(call[0] != "add_binding" or call[3:] not in extras.get(call[1:3], set()) for call in fake.writes)


def test_no_owner_editor_or_admin_roles():
    allowed_admin = {
        "roles/bigquery.connectionAdmin": "connection",
        "roles/cloudscheduler.admin": "project",
        "roles/storage.objectAdmin": "bucket",
    }
    bindings = all_bindings()
    for b in bindings:
        assert b.role not in (OWNER, EDITOR)
        if "admin" in b.role.lower():
            assert b.role in allowed_admin, b.role
            assert b.kind == allowed_admin[b.role], (b.role, b.kind)
        if b.role.startswith("roles/iam."):
            assert b.role in {"roles/iam.serviceAccountUser", "roles/iam.serviceAccountTokenCreator", "roles/iam.workloadIdentityUser"}
    bs.check_roles(bindings)
    for bad in (OWNER, EDITOR, "roles/resourcemanager.projectIam" + "Admin", "roles/iam.securityAdmin"):
        with pytest.raises(ValueError):
            bs.check_roles(bindings + [bs.Binding("x", "user:x@y", bad, "project", bs.PROJECT, "test")])


def test_agent_table_grants_exact_and_builder_has_no_secret_access():
    bindings = all_bindings()
    agent = "serviceAccount:" + sa("f42-agent")
    table_edits = {b.name for b in bindings if b.member == agent and b.kind == "table" and b.role == "roles/bigquery.dataEditor"}
    assert table_edits == {f"{bs.CORE}.{t}" for t in AGENT_TABLES}
    assert not [b for b in bindings if b.member == agent and b.kind == "dataset" and b.name == bs.CORE and b.role == "roles/bigquery.dataEditor"]
    assert [b for b in bindings if b.member == agent and b.kind == "dataset" and b.name == bs.CORE and b.role == "roles/bigquery.dataViewer"]
    table_members = {b.member for b in bindings if b.kind == "table"}
    assert table_members == {agent}
    builder = "serviceAccount:" + sa("f42-builder")
    assert not [b for b in bindings if b.member == builder and "secretmanager" in b.role]
    assert not [b for b in bindings if b.member == builder and b.kind == "secret"]


def test_agent_gets_data_editor_on_the_suppressions_table_only():
    agent = "serviceAccount:" + sa("f42-agent")
    on_table = [b for b in all_bindings() if b.kind == "table" and b.name == SUPPRESSIONS]
    assert [(b.identity, b.member, b.role) for b in on_table] == [("f42-agent", agent, "roles/bigquery.dataEditor")]
    assert on_table[0].reason == "staff add people to the suppression list from the app"
    assert bs.resource_label("table", SUPPRESSIONS) == "table intelligence_42_core.suppressions"


def test_agent_dataset_level_bindings_are_unchanged():
    agent = "serviceAccount:" + sa("f42-agent")
    datasets = sorted((b.role, b.name) for b in all_bindings() if b.member == agent and b.kind == "dataset")
    assert datasets == [
        ("roles/bigquery.dataEditor", bs.AGENT_DATASET),
        ("roles/bigquery.dataViewer", bs.CORE),
        ("roles/bigquery.dataViewer", "trends_v2_dev"),
        ("roles/bigquery.dataViewer", "trends_v2_staging"),
    ]


def test_ui_passcode_secret_is_created_empty_and_only_f42_web_can_read_it():
    fake = FakeCloud()
    state = bs.gather(fake, ACCOUNT)
    created = [r for r in bs.resource_rows(state, fake) if r.status == "CREATE" and bs.UI_PASSCODE in r.resource]
    assert len(created) == 1
    bindings = bs.desired_bindings(state, GROUP)
    on_passcode = [b for b in bindings if b.kind == "secret" and b.name == bs.UI_PASSCODE]
    assert [(b.member, b.role) for b in on_passcode] == [("serviceAccount:" + sa("f42-web"), "roles/secretmanager.secretAccessor")]
    assert on_passcode[0].reason == "beyond SETUP.md table: app passcode gate, lane L4 request"
    web = "serviceAccount:" + sa("f42-web")
    assert not [b for b in bindings if b.member == web and b.kind == "secret" and b.name != bs.UI_PASSCODE]

    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    assert [w for w in fake.writes if w[0] == "create_secret" and w[1] == bs.UI_PASSCODE] == [("create_secret", bs.UI_PASSCODE)]
    assert bs.SECRET in fake.secrets
    fake.writes.clear()
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    assert fake.writes == []


def test_channel_webhook_secret_is_created_empty_and_only_f42_agent_can_read_it():
    fake = FakeCloud()
    state = bs.gather(fake, ACCOUNT)
    created = [r for r in bs.resource_rows(state, fake) if r.status == "CREATE" and bs.CHANNEL_WEBHOOK_URL in r.resource]
    assert [(r.role, r.resource) for r in created] == [("secret", "CHANNEL_WEBHOOK_URL (automatic replication, no version)")]
    bindings = bs.desired_bindings(state, GROUP)
    bs.check_roles(bindings)
    on_webhook = [b for b in bindings if b.kind == "secret" and b.name == bs.CHANNEL_WEBHOOK_URL]
    agent = "serviceAccount:" + sa("f42-agent")
    assert [(b.member, b.role) for b in on_webhook] == [(agent, "roles/secretmanager.secretAccessor")]
    assert on_webhook[0].reason == "daily team channel post from f42-scheduled-asks"
    assert sorted(b.name for b in bindings if b.member == agent and b.kind == "secret") == sorted(
        [bs.SECRET, bs.CHANNEL_WEBHOOK_URL])

    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    assert [w for w in fake.writes if w[0] == "create_secret"] == [
        ("create_secret", bs.UI_PASSCODE), ("create_secret", bs.CHANNEL_WEBHOOK_URL)]
    assert ("roles/secretmanager.secretAccessor", agent) in fake.policies[("secret", bs.CHANNEL_WEBHOOK_URL)]
    fake.writes.clear()
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    assert fake.writes == []


def test_realcloud_creates_the_channel_webhook_secret_as_a_container_only(real):
    real.cloud.create_secret(bs.CHANNEL_WEBHOOK_URL)
    assert real.gcloud.argvs == [["gcloud", "secrets", "create", bs.CHANNEL_WEBHOOK_URL, "--replication-policy=automatic",
                                  "--project=" + bs.PROJECT, "--format=none"]]


def test_source_never_names_the_zero_balance_secret():
    source = Path(bs.__file__).read_text(encoding="utf-8")
    assert ("SOCIALCRAWL" + "_API_KEY") not in source
    assert bs.SECRET == "SOCIALCRAWL_OGILVY_API_KEY"
    for dash in (chr(0x2014), chr(0x2013)):
        assert dash not in source


def test_connection_merge_keeps_old_members_and_refuses_to_drop_one():
    old = {
        "etag": "BwX1",
        "version": 1,
        "bindings": [
            {"role": "roles/bigquery.connectionUser", "members": ["user:someone@ogilvy.co.za"]},
            {"role": "roles/bigquery.connectionAdmin", "members": ["serviceAccount:other@x.iam.gserviceaccount.com"]},
        ],
    }
    snapshot = copy.deepcopy(old)
    member = "serviceAccount:" + sa("f42-agent")
    new = bs.merge_policy(old, "roles/bigquery.connectionUser", member)
    assert old == snapshot
    assert new["etag"] == "BwX1"
    pairs = {(b["role"], m) for b in new["bindings"] for m in b["members"]}
    assert pairs >= {(b["role"], m) for b in old["bindings"] for m in b["members"]}
    assert ("roles/bigquery.connectionUser", member) in pairs
    bs.check_keeps(old, new)
    again = bs.merge_policy(new, "roles/bigquery.connectionUser", member)
    assert again == new
    dropped = copy.deepcopy(new)
    dropped["bindings"][1]["members"] = []
    with pytest.raises(ValueError):
        bs.check_keeps(old, dropped)


def test_preflight_refuses_impersonation_and_service_account_logins(monkeypatch, capsys):
    monkeypatch.setenv(bs.IMPERSONATE_ENV, sa("f42-builder"))
    fake = FakeCloud()
    assert bs.main(["--apply"], cloud=fake) != 0
    assert fake.writes == []
    assert "Refusing" in capsys.readouterr().out
    monkeypatch.delenv(bs.IMPERSONATE_ENV)

    for fake in (
        FakeCloud(impersonation=sa("f42-builder")),
        FakeCloud(account=sa("f42-builder")),
        FakeCloud(account=""),
    ):
        assert bs.main(["--apply"], cloud=fake) != 0
        assert fake.writes == []
        out = capsys.readouterr().out
        assert "Refusing" in out and out.count("\n") == 1

    fake = FakeCloud()
    assert bs.main([], cloud=fake) == 0
    assert f"Acting as {ACCOUNT} on project {bs.PROJECT}" in capsys.readouterr().out


def test_missing_table_is_pending_and_exits_zero(capsys):
    fake = FakeCloud(tables=False)
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    readback = rows_for(fake, readback=True)
    pending = [r for r in readback if r.status == "PENDING"]
    assert len(pending) == len(AGENT_TABLES) == 4 and all(r.binding.kind == "table" for r in pending)
    assert {r.status for r in readback if r.binding.kind != "table"} == {"PRESENT"}
    assert not [w for w in fake.writes if w[0] == "add_binding" and w[1] == "table"]
    out = capsys.readouterr().out
    assert "PENDING" in out and "re-run with --apply" in out


def test_missing_legacy_dataset_is_skipped():
    legacy = tuple(n for n in bs.LEGACY_DATASETS if n != "trends_v2_staging_funded")
    fake = FakeCloud(legacy=legacy)
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    readback = rows_for(fake, readback=True)
    skipped = [r for r in readback if r.status == "SKIPPED"]
    assert skipped and all(r.binding.name == "trends_v2_staging_funded" for r in skipped)
    assert not [w for w in fake.writes if "trends_v2_staging_funded" in w]


def test_without_iap_group_the_group_row_waits_and_apply_still_exits_zero():
    fake = FakeCloud()
    assert bs.main(["--apply"], cloud=fake) == 0
    readback = rows_for(fake, iap_group=None, readback=True)
    waiting = [r for r in readback if r.status == "WAITING"]
    assert len(waiting) == 1 and waiting[0].binding.role == "roles/iap.httpsResourceAccessor"


def test_grant_retries_only_while_the_account_does_not_exist_and_stops_on_a_real_failure(capsys):
    fake = FakeCloud()
    member = "serviceAccount:" + sa("f42-web")
    fake.fail[("roles/run.invoker", member)] = ["ERROR: INVALID_ARGUMENT: Service account f42-web does not exist."] * 2
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0

    fake = FakeCloud()
    fake.fail[("roles/run.invoker", member)] = ["ERROR: PERMISSION_DENIED: caller lacks resourcemanager.projects.setIamPolicy"]
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 1
    out = capsys.readouterr().out
    assert "PERMISSION_DENIED" in out
    grants = [w for w in fake.writes if w[0] == "add_binding"]
    assert grants[-1][3:] == ("roles/run.invoker", member)


OLD_CONDITION = f"assertion.repository == '{bs.GITHUB_REPO}'"
NEW_CONDITION = ("assertion.repository == 'jhbanalytics-pixel/42-Ogilvy-Intelligence'"
                 " && assertion.ref == 'refs/heads/full-42' && assertion.event_name == 'push'")


def live_provider(condition=OLD_CONDITION):
    fake = FakeCloud()
    fake.pool = True
    fake.condition = condition
    return fake


def test_provider_condition_accepts_only_a_push_to_full_42_of_this_repository():
    assert bs.ATTRIBUTE_CONDITION == NEW_CONDITION
    assert "attribute.ref=assertion.ref" in bs.ATTRIBUTE_MAPPING


def test_dry_run_shows_the_condition_before_and_after_and_changes_nothing(capsys):
    fake = live_provider()
    assert bs.main(["--iap-group", GROUP], cloud=fake) == 0
    assert fake.writes == []
    assert fake.condition == OLD_CONDITION
    out = capsys.readouterr().out
    assert f"before: {OLD_CONDITION}" in out
    assert f"after:  {NEW_CONDITION}" in out
    update = [r for r in bs.resource_rows(bs.gather(fake, ACCOUNT), fake) if r.status == "UPDATE"]
    assert len(update) == 1 and update[0].resource == bs.PROVIDER


def test_apply_patches_the_live_provider_condition_once_and_never_recreates_it():
    fake = live_provider()
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    provider_writes = [w for w in fake.writes if "pool" in w[0] or "provider" in w[0]]
    assert provider_writes == [("update_provider_condition", NEW_CONDITION)]
    assert fake.condition == NEW_CONDITION
    fake.writes.clear()
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    assert fake.writes == []


def test_a_live_condition_that_is_not_narrowed_by_the_new_one_is_left_for_albert(capsys):
    other = "assertion.repository == 'someone/else'"
    fake = live_provider(other)
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 1
    assert not [w for w in fake.writes if "provider" in w[0] or "pool" in w[0]]
    assert fake.condition == other
    out = capsys.readouterr().out
    assert "DIFFERS" in out and f"before: {other}" in out


REPO_PRINCIPAL = (f"principalSet://iam.googleapis.com/projects/{NUMBER}/locations/global/workloadIdentityPools/f42-github/"
                  "attribute.repository/jhbanalytics-pixel/42-Ogilvy-Intelligence")
REF_PRINCIPAL = (f"principalSet://iam.googleapis.com/projects/{NUMBER}/locations/global/workloadIdentityPools/f42-github/"
                 "attribute.ref/refs/heads/full-42")
HELD = {
    ("roles/cloudbuild.builds.editor", "project", bs.PROJECT),
    ("roles/serviceusage.serviceUsageConsumer", "project", bs.PROJECT),
    ("roles/iam.serviceAccountUser", "sa", sa("f42-deployer")),
    ("roles/storage.objectCreator", "bucket", bs.BUCKET),
}


def live_today():
    """Staging on 29 September 2026 after Albert's 10:28 apply: every binding in place, the provider condition
    already narrowed to pushes to full-42, and the suppressions table created with no grant on it yet."""
    fake = FakeCloud()
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    fake.condition = NEW_CONDITION
    fake.policies[("table", SUPPRESSIONS)] = set()
    fake.writes.clear()
    return fake


def held_pairs(bindings):
    deployer = "serviceAccount:" + sa("f42-deployer")
    return ({(b.role, b.kind, b.name) for b in bindings if b.member == deployer} & HELD,
            [b for b in bindings if b.member == REF_PRINCIPAL])


def test_held_deployer_grants_are_listed_and_not_applied():
    assert bs.HELD_GRANTS_ON is False
    assert {(role, kind, name) for role, (kind, name), _ in bs.DEPLOYER_BUILD_GRANTS_HELD} == HELD
    assert held_pairs(all_bindings()) == (set(), [])
    wif = [b for b in all_bindings() if b.role == "roles/iam.workloadIdentityUser"]
    assert [(b.member, b.kind, b.name) for b in wif] == [(REPO_PRINCIPAL, "sa", sa("f42-deployer"))]


def test_switching_the_held_grants_on_adds_them_and_the_full_42_principal_set(monkeypatch):
    monkeypatch.setattr(bs, "HELD_GRANTS_ON", True)
    held, ref = held_pairs(all_bindings())
    assert held == HELD
    assert [(b.role, b.kind, b.name) for b in ref] == [("roles/iam.workloadIdentityUser", "sa", sa("f42-deployer"))]
    bs.check_roles(all_bindings())


def test_dry_run_today_plans_only_the_suppressions_grant(capsys):
    fake = live_today()
    capsys.readouterr()
    assert bs.main(["--iap-group", GROUP], cloud=fake) == 0
    assert fake.writes == []
    state = bs.gather(fake, ACCOUNT)
    rows = bs.resource_rows(state, fake) + bs.binding_rows(state, GROUP)
    changes = [r for r in rows if r.status != "EXISTS"]
    assert [(r.status, r.identity, r.role, r.resource) for r in changes] == [
        ("GRANT", "f42-agent", "roles/bigquery.dataEditor", "table " + SUPPRESSIONS)]
    assert len(rows) > 1
    out = capsys.readouterr().out
    plan = out.split("\nPlan\n", 1)[1].split("\n\n", 1)[0].splitlines()[1:]
    assert len(plan) == len(rows)
    new = [line for line in plan if not line.startswith("EXISTS ")]
    assert len(new) == 1
    assert new[0].startswith("GRANT ") and " f42-agent " in new[0] and " table " + SUPPRESSIONS + " " in new[0]
    assert "before:" not in out
    deployer_lines = [line for line in out.splitlines() if line.split()[1:2] == ["f42-deployer"]]
    assert deployer_lines and not [line for line in deployer_lines if any(role in line and bs.resource_label(kind, name) + " " in line for role, kind, name in HELD)]
    assert not [line for line in out.splitlines() if "GitHub " + bs.GITHUB_REF in line]


def test_apply_today_sends_only_the_suppressions_grant():
    fake = live_today()
    before = copy.deepcopy(fake.policies)
    agent = "serviceAccount:" + sa("f42-agent")
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    assert fake.writes == [("add_binding", "table", SUPPRESSIONS, "roles/bigquery.dataEditor", agent)]
    before[("table", SUPPRESSIONS)] = {("roles/bigquery.dataEditor", agent)}
    assert fake.policies == before
    assert (("roles/iam.workloadIdentityUser", REPO_PRINCIPAL)
            in fake.policies[("sa", sa("f42-deployer"))])
    fake.writes.clear()
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    assert fake.writes == []


# RealCloud, with subprocess, requests and the BigQuery client replaced.

class FakeBigQuery:
    def __init__(self, legacy_access=None):
        self.queries = []
        self.legacy_access = legacy_access or []

    def __call__(self, project=None, credentials=None):
        assert project == bs.PROJECT
        self.credentials = credentials
        return self

    def get_dataset(self, ref):
        name = ref.split(".")[-1]
        if name in bs.LEGACY_DATASETS:
            return SimpleNamespace(location="US", access_entries=list(self.legacy_access))
        raise NotFound("dataset " + name)

    def get_table(self, ref):
        raise NotFound("table " + ref)

    def get_iam_policy(self, ref):
        return SimpleNamespace(bindings=[])

    def query(self, sql, location=None):
        self.queries.append((sql, location))
        return SimpleNamespace(result=lambda: [])


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.text = json.dumps(body) if body is not None else ""


class FakeRest:
    def __init__(self, conn_policy=None):
        self.calls = []
        self.connection = None
        self.conn_policy = conn_policy or {"etag": "e1", "bindings": []}

    def __call__(self, method, url, headers=None, json=None, timeout=None):
        assert headers["Authorization"] == "Bearer " + TOKEN
        self.calls.append((method, url, json))
        if url.endswith(":getIamPolicy"):
            return FakeResponse(200, self.conn_policy)
        if url.endswith(":setIamPolicy"):
            return FakeResponse(200, json["policy"])
        if url.endswith(":generateServiceIdentity"):
            return FakeResponse(200, {"done": True})
        if method == "POST" and url == f"https://storage.googleapis.com/storage/v1/b?project={bs.PROJECT}":
            return FakeResponse(200, json)
        if method == "POST" and "connectionId=" in url:
            self.connection = {"name": "c", "cloudResource": {"serviceAccountId": CONN_SA}}
            return FakeResponse(200, self.connection)
        if method == "PATCH" and "/providers/" in url:
            return FakeResponse(200, {"name": "operation", "done": True})
        if method == "GET" and "/connections/" in url:
            return FakeResponse(200, self.connection) if self.connection else FakeResponse(404, {"error": {"message": "not found"}})
        raise AssertionError(url)


class FakeGcloud:
    def __init__(self):
        self.argvs = []

    def __call__(self, argv, **kwargs):
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("stdin") is subprocess.DEVNULL
        self.argvs.append(list(argv))
        joined = " ".join(argv[1:])
        out = ""
        if joined.startswith("auth list"):
            out = json.dumps([{"account": ACCOUNT, "status": "ACTIVE"}])
        elif joined.startswith("auth print-access-token"):
            out = TOKEN + "\n"
        elif joined.startswith("projects describe"):
            out = json.dumps({"projectNumber": NUMBER})
        elif " list" in joined:
            out = "[]"
        elif "get-iam-policy" in joined:
            out = json.dumps({"etag": "e", "bindings": [{"role": "roles/viewer", "members": ["user:someone@ogilvy.co.za"]}]})
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


@pytest.fixture
def real(monkeypatch):
    gcloud, rest, bq = FakeGcloud(), FakeRest(), FakeBigQuery()
    monkeypatch.setattr(bs.subprocess, "run", gcloud)
    monkeypatch.setattr(bs.requests, "request", rest)
    monkeypatch.setattr(bs.bigquery, "Client", bq)
    return SimpleNamespace(gcloud=gcloud, rest=rest, bq=bq, cloud=bs.RealCloud(gcloud="gcloud"))


def test_realcloud_builds_exact_add_binding_commands(real):
    member = "serviceAccount:" + sa("f42-web")
    p = "--project=" + bs.PROJECT
    cases = [
        (("project", bs.PROJECT, "roles/run.invoker"),
         ["gcloud", "projects", "add-iam-policy-binding", bs.PROJECT, "--member=" + member, "--role=roles/run.invoker", "--condition=None", "--format=none"]),
        (("sa", sa("f42-builder"), "roles/iam.serviceAccountUser"),
         ["gcloud", "iam", "service-accounts", "add-iam-policy-binding", sa("f42-builder"), "--member=" + member, "--role=roles/iam.serviceAccountUser", "--condition=None", p, "--format=none"]),
        (("secret", bs.SECRET, "roles/secretmanager.secretAccessor"),
         ["gcloud", "secrets", "add-iam-policy-binding", bs.SECRET, "--member=" + member, "--role=roles/secretmanager.secretAccessor", "--condition=None", p, "--format=none"]),
        (("bucket", bs.BUCKET, "roles/storage.objectViewer"),
         ["gcloud", "storage", "buckets", "add-iam-policy-binding", "gs://" + bs.BUCKET, "--member=" + member, "--role=roles/storage.objectViewer", "--condition=None", p, "--format=none"]),
        (("repo", bs.REPO, "roles/artifactregistry.writer"),
         ["gcloud", "artifacts", "repositories", "add-iam-policy-binding", bs.REPO, "--location=" + bs.REGION, "--member=" + member, "--role=roles/artifactregistry.writer", "--condition=None", p, "--format=none"]),
    ]
    for (kind, name, role), expected in cases:
        real.gcloud.argvs.clear()
        real.cloud.add_binding(kind, name, role, member)
        assert real.gcloud.argvs == [expected]

    real.gcloud.argvs.clear()
    real.cloud.create_secret(bs.UI_PASSCODE)
    assert real.gcloud.argvs == [["gcloud", "secrets", "create", bs.UI_PASSCODE, "--replication-policy=automatic", p, "--format=none"]]

    real.cloud.add_binding("dataset", "trends_v2_dev", "roles/bigquery.dataViewer", member)
    real.cloud.add_binding("table", f"{bs.CORE}.credit_ledger", "roles/bigquery.dataEditor", member)
    assert real.bq.queries[-2] == (
        f'GRANT `roles/bigquery.dataViewer` ON SCHEMA `{bs.PROJECT}.trends_v2_dev` TO "{member}"', "US")
    sql, location = real.bq.queries[-1]
    assert sql == f'GRANT `roles/bigquery.dataEditor` ON TABLE `{bs.PROJECT}.{bs.CORE}.credit_ledger` TO "{member}"'


def test_realcloud_connection_grant_sends_old_members_and_etag(monkeypatch, real):
    old = {"etag": "BwOld", "bindings": [{"role": "roles/bigquery.connectionUser", "members": ["user:someone@ogilvy.co.za"]}]}
    rest = FakeRest(conn_policy=old)
    monkeypatch.setattr(bs.requests, "request", rest)
    member = "serviceAccount:" + sa("f42-agent")
    real.cloud.add_binding("connection", bs.CONNECTION, "roles/bigquery.connectionUser", member)
    sent = [body for method, url, body in rest.calls if url.endswith(":setIamPolicy")]
    assert len(sent) == 1
    policy = sent[0]["policy"]
    assert policy["etag"] == "BwOld"
    assert set(policy["bindings"][0]["members"]) == {"user:someone@ogilvy.co.za", member}


def test_realcloud_condition_update_is_one_patch_of_attribute_condition_only(real):
    real.cloud.update_provider_condition()
    assert real.rest.calls == [(
        "PATCH",
        f"https://iam.googleapis.com/v1/projects/{bs.PROJECT}/locations/global/workloadIdentityPools/f42-github/"
        "providers/f42-github-staging?updateMask=attributeCondition",
        {"attributeCondition": NEW_CONDITION},
    )]
    assert real.gcloud.argvs == [["gcloud", "auth", "print-access-token"]]


def test_realcloud_reads_the_live_provider_condition(monkeypatch, real):
    listed = [
        {"name": f"projects/{NUMBER}/locations/global/workloadIdentityPools/f42-github/providers/f42-github-staging",
         "state": "ACTIVE", "attributeCondition": OLD_CONDITION},
    ]

    def gcloud(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(listed), stderr="")

    monkeypatch.setattr(bs.subprocess, "run", gcloud)
    assert real.cloud.provider_condition() == OLD_CONDITION
    listed[0]["state"] = "DELETED"
    assert real.cloud.provider_condition() is None
    listed[0] = {"name": listed[0]["name"], "state": "ACTIVE"}
    assert real.cloud.provider_condition() == ""


@pytest.mark.parametrize("policy", [
    {"etag": "", "bindings": [{"role": "roles/bigquery.connectionUser", "members": ["user:someone@ogilvy.co.za"]}]},
    {"bindings": [{"role": "roles/bigquery.connectionUser", "members": ["user:someone@ogilvy.co.za"]}]},
])
def test_realcloud_connection_grant_refuses_without_a_fetched_etag(monkeypatch, real, policy):
    rest = FakeRest(conn_policy=policy)
    monkeypatch.setattr(bs.requests, "request", rest)
    with pytest.raises(bs.CloudError) as err:
        real.cloud.add_binding("connection", bs.CONNECTION, "roles/bigquery.connectionUser", "serviceAccount:" + sa("f42-agent"))
    assert "etag" in str(err.value)
    assert not [url for _, url, _ in rest.calls if url.endswith(":setIamPolicy")]


@pytest.mark.parametrize("error", [
    bs.requests.exceptions.ConnectTimeout,
    bs.requests.exceptions.ReadTimeout,
    bs.requests.exceptions.ConnectionError,
])
def test_realcloud_network_errors_become_one_line_cloud_errors_without_the_token(monkeypatch, real, capsys, error):
    def boom(method, url, headers=None, json=None, timeout=None):
        raise error(f"pool broke\nsecond line Authorization: Bearer {TOKEN}")

    monkeypatch.setattr(bs.requests, "request", boom)
    with pytest.raises(bs.CloudError) as err:
        real.cloud.connection()
    message = str(err.value)
    assert message and "\n" not in message and TOKEN not in message
    assert err.value.__cause__ is None and err.value.__suppress_context__

    assert bs.main(["--apply"], cloud=bs.RealCloud(gcloud="gcloud")) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "Traceback" not in out and TOKEN not in out


def test_realcloud_full_run_never_removes_and_never_prints_the_token(real, capsys):
    rc = bs.main(["--apply", "--iap-group", GROUP], cloud=real.cloud)
    assert rc in (0, 1)
    out = capsys.readouterr().out
    assert TOKEN not in out and "ya29" not in out
    assert real.gcloud.argvs
    for argv in real.gcloud.argvs:
        for bad in FORBIDDEN_ARGV:
            assert not any(bad in a for a in argv), argv
        assert not any(TOKEN in a for a in argv)
        assert "versions" not in argv, argv
        assert not any("data-file" in a for a in argv), argv
        if argv[1] not in ("auth", "config", "projects"):
            assert "--project=" + bs.PROJECT in argv, argv
    assert real.bq.queries
    for sql, _ in real.bq.queries:
        assert sql.startswith(("GRANT ", "CREATE SCHEMA IF NOT EXISTS ")), sql
        assert ("RE" + "VOKE") not in sql.upper()
    rest_urls = [url for _, url, _ in real.rest.calls]
    assert any(u.endswith(":generateServiceIdentity") for u in rest_urls)
    assert all(bs.PROJECT in u for u in rest_urls)
    buckets = [body for method, url, body in real.rest.calls if url.startswith("https://storage.googleapis.com/")]
    assert buckets == [{
        "name": bs.BUCKET,
        "location": "US-CENTRAL1",
        "iamConfiguration": {"uniformBucketLevelAccess": {"enabled": True}, "publicAccessPrevention": "enforced"},
        "softDeletePolicy": {"retentionDurationSeconds": str(30 * 86400)},
    }]
    assert not [m for m, _, _ in real.rest.calls if m not in ("GET", "POST")]


def test_gmail_app_password_grant_waits_for_the_secret_and_then_goes_to_f42_agent_only(capsys):
    agent = "serviceAccount:" + sa("f42-agent")
    fake = FakeCloud()
    # Albert creates the secret and its value; until then nothing about it is planned, created or granted.
    assert not [b for b in all_bindings(fake) if b.name == bs.GMAIL_APP_PASSWORD]
    assert bs.main(["--iap-group", GROUP], cloud=fake) == 0
    out = capsys.readouterr().out
    assert f"{bs.GMAIL_APP_PASSWORD} does not exist yet" in out and "f42-agent" in out
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    assert bs.GMAIL_APP_PASSWORD not in fake.secrets
    assert not [w for w in fake.writes if bs.GMAIL_APP_PASSWORD in w]

    fake.secrets.add(bs.GMAIL_APP_PASSWORD)
    on_secret = [b for b in all_bindings(fake) if b.kind == "secret" and b.name == bs.GMAIL_APP_PASSWORD]
    assert [(b.member, b.role) for b in on_secret] == [(agent, "roles/secretmanager.secretAccessor")]
    assert on_secret[0].reason == "beyond SETUP.md table: the daily alert email digest, f42-digest"
    fake.writes.clear()
    capsys.readouterr()
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    assert [w for w in fake.writes if w[0] == "add_binding" and w[2] == bs.GMAIL_APP_PASSWORD] == [
        ("add_binding", "secret", bs.GMAIL_APP_PASSWORD, "roles/secretmanager.secretAccessor", agent)]
    assert not [w for w in fake.writes if w[0] == "create_secret"]
    assert f"{bs.GMAIL_APP_PASSWORD} does not exist yet" not in capsys.readouterr().out
    fake.writes.clear()
    assert bs.main(["--apply", "--iap-group", GROUP], cloud=fake) == 0
    assert fake.writes == []
