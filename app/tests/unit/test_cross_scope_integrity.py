"""E03: scope isolation and digest integrity at the real application boundary.

The server owns the scope. Scope A is the configured default, ogilvy_default;
scope B is another configured client scope, bsa_pulse. Every test drives the
real FastAPI application through the test client, with storage replaced by the
in-memory fixtures the existing route tests use.

Cross scope: a record of scope B requested by id, by a guessed id and through a
listing gets the same refusal as an id that does not exist, and the refusal
carries none of the record's content.

Digest integrity: an artifact version, a review resource version or a worker
task digest with one hex character changed, the wrong length, upper case, an
algorithm prefix, or a well formed stale value is refused with a named code.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from src.api import (
    general_question_routes,
    investigation_index,
    main,
    workspace_scope,
)
from src.api.question_worker_process import WorkerProcessError
from tests.unit import test_main_dossier_review_routes as review_routes
from tests.unit.test_boundary_certification import (
    client,
    record_name,
    scope_keyed_reads,
    scoped_record,
)
from tests.unit.test_main_artifact_routes import prepared, read_url
from tests.unit.test_main_dossier_review_routes import (
    SUBJECTS,
    claim_command,
    detail,
    reviewer,
)
from tests.unit.test_question_worker_result import EXPECTED
from tests.unit.test_workspace_scope import Bucket as RecordBucket
from tests.unit.test_workspace_scope import stored_record

EXECUTE = "/internal/general-question/execute"
FOREIGN_SCOPE = "bsa_pulse"
GUESSED_ID = "inv_" + "0" * 32
REFUSAL_HEADERS = ("cache-control", "content-type", "vary", "x-content-type-options")


@pytest.fixture
def open_gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


def fingerprint(response):
    return (
        response.status_code,
        response.content,
        tuple(response.headers.get(name) for name in REFUSAL_HEADERS),
    )


def private_text(record):
    """Text only the owning scope may see; a refusal must echo none of it."""
    frame = record["frame"]
    return [
        frame["decision_question"],
        frame["run_id"],
        frame["client_scope_id"],
        record["response"]["created_at"],
    ]


# Cross scope: status, history, dossier and export on the investigation surface


def test_a_foreign_record_an_absent_id_and_a_guessed_id_get_one_identical_refusal(
    monkeypatch, open_gate
):
    record = stored_record(client_scope_id=FOREIGN_SCOPE, brand_config_id="bsa")
    foreign_id, bucket = scoped_record(monkeypatch, FOREIGN_SCOPE, "bsa")
    browser = client()
    foreign = scope_keyed_reads(browser, foreign_id)
    guessed = scope_keyed_reads(browser, GUESSED_ID)
    monkeypatch.setattr(workspace_scope, "_workspace_bucket", lambda: RecordBucket())
    absent = scope_keyed_reads(browser, foreign_id)
    assert set(foreign) == {
        "status",
        "claims",
        "decision",
        "history",
        "dossier",
        "export",
    }
    for name, response in foreign.items():
        assert response.status_code == 404, (name, response.text)
        assert fingerprint(response) == fingerprint(absent[name]), name
        assert fingerprint(response) == fingerprint(guessed[name]), name
        for text in private_text(record):
            assert text not in response.text, (name, text)
            assert all(text not in value for value in response.headers.values()), name
        assert "set-cookie" not in response.headers
    # The foreign record is read once per request, on its own binding, and no
    # dependent private read follows a refusal.
    assert bucket.lookups.count(record_name(foreign_id)) == len(foreign)


def test_the_foreign_record_is_the_one_the_server_scope_would_have_served(
    monkeypatch, open_gate
):
    """The refusal above is the scope check: the same bytes under scope A are served."""
    own_id, _bucket = scoped_record(monkeypatch, "ogilvy_default")
    record = stored_record()
    served = client().post(f"/api/v2/investigations/{own_id}/status")
    assert served.status_code == 200, served.text
    assert record["frame"]["run_id"] in served.text


def test_the_investigation_listing_never_names_a_foreign_record(monkeypatch, open_gate):
    own = stored_record()
    foreign = stored_record(client_scope_id=FOREIGN_SCOPE, brand_config_id="bsa")
    foreign["frame"]["decision_question"] = "Scope B private decision question"
    monkeypatch.setattr(main, "_investigation_bucket", lambda: object())
    monkeypatch.setattr(
        investigation_index,
        "read_stored_investigations",
        lambda **_: [
            {
                "investigation_id": item["response"]["investigation_id"],
                "client_scope_id": item["frame"]["client_scope_id"],
                "decision_question": item["frame"]["decision_question"],
                "market_scope": item["frame"]["market_scope"],
                "created_at": item["response"]["created_at"],
            }
            for item in (own, foreign)
        ],
    )
    response = client().post(
        "/api/v2/investigations/list",
        params={"contract_version": investigation_index.CONTRACT_VERSION},
    )
    assert response.status_code == 200, response.text
    listed = [row["investigation_id"] for row in response.json()["investigations"]]
    assert listed == [own["response"]["investigation_id"]]
    assert response.json()["total_count"] == 1
    assert "Scope B private decision question" not in response.text
    assert FOREIGN_SCOPE not in response.text


# Cross scope: the Ask status, history and export surface


class ScopedWorker:
    """Stands in for the engine process with the refusal codes the engine returns.

    The engine side of this boundary is held in engine/tests/unit/
    test_scope_and_digest_boundary.py; here the app must pass only the server
    scope and must not let the engine's reason reach the response.
    """

    storage_client = None

    def __init__(self, reason):
        self.reason = reason
        self.payloads = []

    async def run(self, operation, payload, *, deadline):
        self.payloads.append((operation, payload))
        raise WorkerProcessError(self.reason)


def install_worker(monkeypatch, worker):
    service = general_question_routes.GeneralQuestionRoutes(
        worker,
        None,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )
    monkeypatch.setattr(
        main.app.state, "general_question_routes", service, raising=False
    )
    return service


CALLER_SCOPE_HINTS = {
    "params": {"client_scope_id": FOREIGN_SCOPE, "brand_config_id": "bsa"},
    "headers": {"X-Client-Scope": FOREIGN_SCOPE, "X-Brand-Config": "bsa"},
    "cookies": {"client_scope_id": FOREIGN_SCOPE},
}


def ask_reads(browser, request_id):
    job = "chat_" + request_id.replace("-", "")
    return {
        "status": browser.get("/api/chat/status", params={"job_id": job}),
        "export_html": browser.get(f"/api/chat/answer/{request_id}/export.html"),
        "export_pdf": browser.get(f"/api/chat/answer/{request_id}/export.pdf"),
    }


@pytest.mark.parametrize(
    "foreign_reason, absent_reason",
    [
        # The worker process reports every non zero exit of the status child
        # the same way, whether the engine said scope_invalid or request_unknown.
        ("worker_exit_failed", "worker_exit_failed"),
        ("observation_request_unavailable", "observation_request_unavailable"),
    ],
)
def test_ask_reads_of_a_foreign_request_match_an_absent_one_and_leak_nothing(
    monkeypatch, open_gate, foreign_reason, absent_reason
):
    foreign_id = "00000000-0000-4000-8000-00000000000a"
    absent_id = "00000000-0000-4000-8000-00000000000b"
    browser = client()
    foreign_worker = ScopedWorker(foreign_reason)
    install_worker(monkeypatch, foreign_worker)
    foreign = ask_reads(browser, foreign_id)
    install_worker(monkeypatch, ScopedWorker(absent_reason))
    absent = ask_reads(browser, absent_id)
    for name, response in foreign.items():
        assert response.status_code in (404, 503), (name, response.text)
        assert response.status_code == absent[name].status_code, name
        assert response.json() == absent[name].json(), name
        assert foreign_reason not in response.text
        assert "scope" not in response.text
        assert foreign_id not in response.text
        assert foreign_id[:8] not in response.headers.get("content-disposition", "")


def test_ask_reads_send_only_the_server_scope_whatever_the_caller_names(
    monkeypatch, open_gate
):
    request_id = "00000000-0000-4000-8000-00000000000a"
    worker = ScopedWorker("observation_request_unavailable")
    install_worker(monkeypatch, worker)
    browser = client()
    job = "chat_" + request_id.replace("-", "")
    browser.get(
        "/api/chat/status",
        params={"job_id": job},
        headers=CALLER_SCOPE_HINTS["headers"],
        cookies=CALLER_SCOPE_HINTS["cookies"],
    )
    browser.get(
        f"/api/chat/answer/{request_id}/export.html",
        params=CALLER_SCOPE_HINTS["params"],
        headers=CALLER_SCOPE_HINTS["headers"],
        cookies=CALLER_SCOPE_HINTS["cookies"],
    )
    server_scope = general_question_routes._current_scope()
    assert server_scope["client_scope_id"] == "ogilvy_default"
    assert [operation for operation, _ in worker.payloads] == ["status", "observe"]
    for _operation, payload in worker.payloads:
        assert payload["scope"] == server_scope
        assert payload["request_id"] == request_id


def test_the_ask_status_route_rejects_a_widened_job_identity_before_the_worker(
    monkeypatch, open_gate
):
    worker = ScopedWorker("worker_exit_failed")
    install_worker(monkeypatch, worker)
    browser = client()
    for job in (
        "chat_" + "0" * 31,
        "chat_" + "0" * 33,
        "chat_" + "A" * 32,
        "chat_*",
        "chat_" + "0" * 32 + "\n",
    ):
        response = browser.get("/api/chat/status", params={"job_id": job})
        assert response.status_code == 400, (job, response.text)
    assert worker.payloads == []


# Altered artifact version on the export read


@pytest.fixture
def review_environment(monkeypatch):
    yield from review_routes.review_environment.__wrapped__(monkeypatch)


@pytest.fixture
def workspace(monkeypatch, review_environment):
    return review_routes.workspace.__wrapped__(monkeypatch)


def flip_one(value):
    return value[:-1] + ("0" if value[-1] != "0" else "1")


DIGEST_VARIANTS = {
    "one_hex_changed": flip_one,
    "short": lambda value: value[:-1],
    "long": lambda value: value + "0",
    "uppercase": str.upper,
    "algorithm_prefix": lambda value: "sha256:" + value,
    "algorithm_prefix_sha512": lambda value: "sha512:" + value,
}


def test_an_altered_artifact_version_is_refused_like_an_absent_artifact(
    monkeypatch, workspace
):
    _editor, _ready, result = prepared(monkeypatch, workspace)
    version = result["artifact_version"]
    assert any(character.isalpha() for character in version)
    reader = client()
    url = read_url(workspace, result["artifact_id"])
    absent = reader.post(
        read_url(workspace, "art_0000000000000000"),
        params={"artifact_version": version},
    )
    assert (absent.status_code, detail(absent)["code"]) == (404, "scope_invalid")
    for name, change in DIGEST_VARIANTS.items():
        response = reader.post(url, params={"artifact_version": change(version)})
        assert fingerprint(response) == fingerprint(absent), name
        assert "html" not in response.text, name
    served = reader.post(url, params={"artifact_version": version})
    assert served.status_code == 200, served.text


def test_a_stale_artifact_version_is_refused_after_the_artifact_moves_on(
    monkeypatch, workspace
):
    """The version a caller held before the stored record changed is a stale generation."""
    _editor, _ready, result = prepared(monkeypatch, workspace)
    stale = result["artifact_version"]
    monkeypatch.setattr(
        main.dossier_artifacts,
        "read_artifact",
        lambda **kwargs: {
            "artifact_id": result["artifact_id"],
            "artifact_version": flip_one(stale),
            "manifest": {"scope_digest": workspace.scope.scope_digest},
        },
    )
    response = client().post(
        read_url(workspace, result["artifact_id"]), params={"artifact_version": stale}
    )
    assert (response.status_code, detail(response)["code"]) == (404, "scope_invalid")


# Altered resource version on a review command


@pytest.mark.parametrize("variant", sorted(DIGEST_VARIANTS))
def test_an_altered_review_resource_version_is_refused_and_writes_nothing(
    monkeypatch, workspace, variant
):
    browser, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    genuine = claim_command(workspace)["resource_version"]
    baseline = list(workspace.bucket.uploads)
    command = claim_command(
        workspace, resource_version=DIGEST_VARIANTS[variant](genuine)
    )
    response = browser.post(workspace.review, json=command, headers=ready)
    # Malformed text fails the command grammar; a well formed but different
    # digest is compared with the stored resource and is a version conflict.
    expected = (
        (409, "review_version_conflict")
        if variant == "one_hex_changed"
        else (400, "review_request_invalid")
    )
    assert (response.status_code, detail(response)["code"]) == expected, response.text
    assert workspace.bucket.uploads == baseline


def test_the_genuine_review_resource_version_is_accepted(monkeypatch, workspace):
    """The refusals above are the version check and not a dead harness."""
    browser, ready = reviewer(monkeypatch, SUBJECTS["claims"])
    response = browser.post(
        workspace.review, json=claim_command(workspace), headers=ready
    )
    assert response.status_code == 200, response.text


# Altered digest on the worker task boundary


def worker_task_context(monkeypatch):
    monkeypatch.setattr(
        general_question_routes,
        "verify_question_worker_authorization",
        lambda *args, **kwargs: {},
    )
    reads = []

    def read_request_bytes(*args, **kwargs):
        reads.append(kwargs["invocation"])
        raise AssertionError("the request store was read")

    monkeypatch.setattr(
        general_question_routes, "read_request_bytes", read_request_bytes
    )
    install_worker(monkeypatch, SimpleNamespace(storage_client=None))
    return reads


def parent_failures(caplog):
    rows = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith('{"code":'):
            row = json.loads(message)
            if row.get("state") == "failed":
                rows.append(row)
    return rows


TASK_DIGESTS = ("request_digest", "intake_digest", "policy_digest", "deployment_digest")
MALFORMED = {
    name: change
    for name, change in DIGEST_VARIANTS.items()
    if name != "one_hex_changed"
}


@pytest.mark.parametrize("variant", sorted(MALFORMED))
@pytest.mark.parametrize("field", TASK_DIGESTS)
def test_a_malformed_task_digest_is_refused_before_the_store_with_a_named_code(
    monkeypatch, caplog, field, variant
):
    reads = worker_task_context(monkeypatch)
    altered = EXPECTED | {field: MALFORMED[variant](EXPECTED[field])}
    with caplog.at_level("INFO"):
        response = client().post(EXECUTE, json=altered)
    assert response.status_code in (400, 403, 503), response.text
    assert EXPECTED[field] not in response.text
    assert reads == []
    failures = parent_failures(caplog)
    assert [row["code"] for row in failures] == ["worker_input_invalid"]


@pytest.mark.parametrize("field", ("policy_digest", "deployment_digest"))
@pytest.mark.parametrize("stale", ("one_hex_changed", "previous_generation"))
def test_a_well_formed_but_inactive_binding_digest_is_refused_with_a_named_code(
    monkeypatch, caplog, field, stale
):
    reads = worker_task_context(monkeypatch)
    value = flip_one(EXPECTED[field]) if stale == "one_hex_changed" else "0" * 64
    with caplog.at_level("INFO"):
        response = client().post(EXECUTE, json=EXPECTED | {field: value})
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "Worker binding is not active"}
    assert reads == []
    failures = parent_failures(caplog)
    assert [row["code"] for row in failures] == ["worker_binding_invalid"]


def test_the_exact_task_digests_pass_the_binding_check(monkeypatch, caplog):
    """With every digest exact the task reaches the request store: the gate is the digest."""
    reads = worker_task_context(monkeypatch)
    with caplog.at_level("INFO"):
        response = client().post(EXECUTE, json=EXPECTED)
    assert response.status_code == 503, response.text
    assert reads == [EXPECTED]
    assert [row["operation"] for row in parent_failures(caplog)] == ["request_read"]
