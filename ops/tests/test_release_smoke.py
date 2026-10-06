"""The tier one release smoke check: every check passes and fails on its own
evidence, the access key never leaves the request header, no request that could
submit a question or reserve allowance can be sent, and the receipt only grows.

Both HTTP layers are fakes: the staging app is a table of GET responses and the
native reads go through the release adapter over a fake transport.
"""

import copy
import io
import json
import re
import urllib.parse
from datetime import UTC, datetime

import pytest
from ops.deploy import release
from ops.runners import release_smoke
from ops.deploy import release_native_adapter as native
from ops.tests.test_foundation_release import (
    LP_COMMIT,
    ROOT,
    SERVICE_API,
    Scenario,
    make_request_row,
    make_revision,
    make_service,
)
from ops.tests.test_release_index import make_index

STAGING = "https://staging.example"
KEY = "sentinel-access-key-7Qx9"
RID = "8f2c3b4a-5d6e-4f70-8a9b-0c1d2e3f4a5b"
OTHER_RID = "11111111-2222-4333-8444-555555555555"
WINDOW = {"start": "2026-09-01", "end": "2026-09-14"}
NOW = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
MAIN = ROOT / "app" / "src" / "api" / "main.py"


def _json(status, value, headers=None):
    return {
        "status": status,
        "headers": {"Content-Type": "application/json", **(headers or {})},
        "body": json.dumps(value).encode("utf-8"),
    }


def coverage(start=WINDOW["start"], end=WINDOW["end"], state="covered"):
    return {
        "contract_version": "general_question_coverage_v1",
        "state": state,
        "window": {"start": start, "end": end} if state == "covered" else None,
        "cutoff_date": end if state == "covered" else None,
    }


def detail(request_id=RID, state="complete"):
    receipts = [
        {"receipt_id": "rc-1", "citation_label": "R1", "kind": "content"},
        {"receipt_id": "rc-2", "citation_label": "R2", "kind": "content"},
        {"receipt_id": "rc-3", "citation_label": "R3", "kind": "aggregate"},
    ]
    return {
        "contract_version": "general_question_detail_v1",
        "request_id": request_id,
        "observed_state": state,
        "question": "What stood out in South Africa in the latest available week?",
        "history": [],
        "selected_market": "za",
        "requested_window": None,
        "response": {
            "answer": "A short read.",
            "sources": [],
            "intelligence": {
                "contract_version": "general_cultural_question_v1",
                "request_id": request_id,
                "status": state,
                "claims": [
                    {"claim_id": "c1", "receipt_ids": ["rc-1"], "reading_ids": []},
                    {"claim_id": "c2", "receipt_ids": [], "reading_ids": ["rd-1"]},
                ],
                "receipts": receipts,
                "readings": [{"reading_id": "rd-1", "source_receipt_ids": ["rc-2"]}],
            },
        },
        "window_from_plan": True,
        "reserved_microusd": 100_000,
        "missing_work": [],
    }


def export_page(request_id=RID, title=None, heading="Ask answer"):
    title = title if title is not None else "42 Ask answer " + request_id[:8]
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{title}</title><style>h1{{}}</style></head><body><header>"
        '<p class="brand">42 Ogilvy Intelligence</p>'
        f"<h1>{heading}</h1><p>Question: q</p></header><main></main></body></html>"
    ).encode("utf-8")


def export_response(request_id=RID, filename=None, **page):
    filename = filename or f"42-answer-{request_id[:8]}.html"
    return {
        "status": 200,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
        "body": export_page(request_id, **page),
    }


class Staging:
    """The staging app: one response per allowlisted GET, every request kept."""

    def __init__(self):
        self.requests = []
        self.routes = {
            "/api/chat/coverage": _json(200, coverage()),
            "/api/internal/v2/fieldwork/question": _json(200, detail()),
            f"/api/chat/answer/{RID}/export.html": export_response(),
        }
        self.raise_error = None

    def __call__(self, request):
        self.requests.append(copy.deepcopy(request))
        if self.raise_error is not None:
            raise self.raise_error
        path = request["url"].removeprefix(STAGING).split("?", 1)[0]
        return copy.deepcopy(self.routes.get(path, _json(404, {"detail": "Not Found"})))


class Cloud:
    """Cloud Run Admin and storage JSON reads for one serving revision."""

    def __init__(self, scenario):
        self.requests = []
        self.service = make_service(scenario.binding, scenario.tag)
        self.revision = make_revision(scenario.binding, scenario.tag)
        self.ledger = copy.deepcopy(scenario.ledger)

    def __call__(self, request):
        self.requests.append({"method": request["method"], "url": request["url"]})
        url = request["url"]
        name = self.revision["name"]
        if url == native.RUN_ENDPOINT + SERVICE_API:
            return _json(200, self.service)
        if url == native.RUN_ENDPOINT + name:
            return _json(200, self.revision)
        raw = json.dumps(self.ledger).encode("utf-8")
        if url == native._object_url(release.BUCKET, release.LEDGER_OBJECT):
            return _json(200, {"generation": "77", "size": str(len(raw))})
        if url == native._object_url(
            release.BUCKET, release.LEDGER_OBJECT, alt="media", generation="77"
        ):
            return {"status": 200, "headers": {}, "body": raw}
        return _json(404, {"error": {"code": 404}})


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "_adc_token", lambda: "native-bearer")
    scenario = Scenario(tmp_path)
    index, _references = make_index(scenario)
    record = tmp_path / "release_index.json"
    record.write_text(json.dumps(index), encoding="utf-8")
    key_file = tmp_path / "access.key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    world = type("World", (), {})()
    world.tmp = tmp_path
    world.scenario = scenario
    world.index = index
    world.record = record
    world.key_file = key_file
    world.receipt = tmp_path / "smoke.jsonl"
    world.staging = Staging()
    world.cloud = Cloud(scenario)
    return world


def run(world, *extra, window="2026-09-01/2026-09-14", request_id=RID, **overrides):
    argv = {
        "--staging-url": STAGING,
        "--release-record": str(world.record),
        "--expected-window": window,
        "--request-id": request_id,
        "--access-key-file": str(world.key_file),
        "--receipt": str(world.receipt),
    }
    argv.update(overrides)
    out, err = io.StringIO(), io.StringIO()
    code = release_smoke.execute(
        [item for pair in argv.items() for item in pair] + list(extra),
        transport=world.staging,
        native_reads=release_smoke.native_clients(world.cloud),
        now=NOW,
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue()


def receipt_lines(world):
    return [json.loads(line) for line in world.receipt.read_text().splitlines()]


# Pass


def test_every_check_passes_and_one_receipt_line_carries_the_observed_values(world):
    code, out, err = run(world)
    assert code == 0, out + err
    [line] = receipt_lines(world)
    assert line["overall_status"] == "pass"
    assert line["checked_at"] == "2026-09-25T06:00:00Z"
    assert line["staging_url"] == STAGING
    assert line["release_commit"] == LP_COMMIT
    checks = line["checks"]
    assert set(checks) == set(release_smoke.CHECKS)
    assert all(item["status"] == "pass" for item in checks.values())
    revision = checks["revision"]["observed"]
    assert revision["revision"] == world.scenario.binding["revision_name"]
    assert revision["app_image"] == world.index["app_image"]
    assert revision["source_sha"] == LP_COMMIT
    assert (
        checks["revision"]["expected"]["engine_image_digest"]
        == (world.index["engine_image"].rsplit("@sha256:", 1)[1])
    )
    assert checks["coverage"]["observed"]["start"] == "2026-09-01"
    assert checks["coverage"]["observed"]["end"] == "2026-09-14"
    answer = checks["saved_answer"]["observed"]
    assert (answer["claims"], answer["citations"], answer["source_records"]) == (
        2,
        2,
        3,
    )
    assert answer["citation_labels"] == ["R1", "R2"]
    exported = checks["export"]["observed"]
    assert exported["filename"] == "42-answer-8f2c3b4a.html"
    assert exported["title"] == "42 Ask answer 8f2c3b4a"
    assert exported["heading"] == "Ask answer"
    allowance = checks["allowance"]["observed"]
    assert (allowance["used"], allowance["limit"], allowance["remaining"]) == (
        0,
        100,
        100,
    )
    assert "release smoke pass" in out and err == ""


def test_the_run_sends_exactly_the_three_allowlisted_reads_and_only_native_gets(world):
    run(world)
    sent = [
        (item["method"], item["url"].removeprefix(STAGING))
        for item in world.staging.requests
    ]
    assert sent == [
        ("GET", "/api/chat/coverage"),
        (
            "GET",
            (
                "/api/internal/v2/fieldwork/question?contract_version="
                f"general_question_detail_v1&request_id={RID}"
            ),
        ),
        ("GET", f"/api/chat/answer/{RID}/export.html"),
    ]
    assert all(item["headers"]["X-Passcode"] == KEY for item in world.staging.requests)
    assert {item["method"] for item in world.cloud.requests} == {"GET"}


def test_allowance_counts_the_admitted_requests_on_the_ledger(world):
    admitted = datetime(2026, 9, 20, tzinfo=UTC)
    world.cloud.ledger["requests"] = {
        RID: make_request_row(
            world.scenario.policy, world.scenario.binding, admitted_at=admitted
        ),
        OTHER_RID: make_request_row(
            world.scenario.policy, world.scenario.binding, admitted_at=admitted
        ),
    }
    world.cloud.ledger["reserved_microusd"] = 200_000
    assert run(world)[0] == 0
    observed = receipt_lines(world)[0]["checks"]["allowance"]["observed"]
    assert (observed["used"], observed["remaining"], observed["reserved_microusd"]) == (
        2,
        98,
        200_000,
    )


# Each failure


def _other_image(world):
    world.cloud.revision["containers"][0]["image"] = (
        release.APP_IMAGE_NAME + "@sha256:" + "e" * 64
    )


def _other_commit(world):
    for item in world.cloud.revision["containers"][0]["env"]:
        if item["name"] == "SOURCE_SHA":
            item["value"] = "f" * 40


def _other_deployment(world):
    for item in world.cloud.revision["containers"][0]["env"]:
        if item["name"] == "GENERAL_QUESTION_DEPLOYMENT_DIGEST":
            item["value"] = "a" * 64


def _not_ready(world):
    world.cloud.revision["conditions"] = [
        {"type": "Ready", "state": "CONDITION_FAILED"}
    ]


def _split_traffic(world):
    world.cloud.service["trafficStatuses"][0]["percent"] = 50


def _route(path, response):
    def edit(world):
        world.staging.routes[path] = response

    return edit


def _detail(edit):
    def apply(world):
        value = detail()
        edit(value)
        world.staging.routes["/api/internal/v2/fieldwork/question"] = _json(200, value)

    return apply


def _drop_citation(value):
    value["response"]["intelligence"]["claims"][0]["receipt_ids"] = ["rc-missing"]


def _no_sources(value):
    intelligence = value["response"]["intelligence"]
    intelligence["receipts"] = []
    intelligence["claims"] = [{"claim_id": "c1", "receipt_ids": [], "reading_ids": []}]
    intelligence["readings"] = []


def _no_claims(value):
    value["response"]["intelligence"]["claims"] = []


def _bad_label(value):
    value["response"]["intelligence"]["receipts"][0]["citation_label"] = "S1"


def _uncited(value):
    for claim in value["response"]["intelligence"]["claims"]:
        claim["receipt_ids"], claim["reading_ids"] = [], []


def _ledger_inconsistent(world):
    world.cloud.ledger["reserved_microusd"] = 100_000


EXPORT = f"/api/chat/answer/{RID}/export.html"
DETAIL = "/api/internal/v2/fieldwork/question"
FAILURES = {
    "revision_image": ("revision", "app_image_mismatch", _other_image),
    "revision_commit": ("revision", "commit_mismatch", _other_commit),
    "revision_deployment": (
        "revision",
        "deployment_digest_mismatch",
        _other_deployment,
    ),
    "revision_ready": ("revision", "revision_not_ready", _not_ready),
    "revision_traffic": ("revision", "no_single_serving_revision", _split_traffic),
    "coverage_window": (
        "coverage",
        "window_mismatch",
        _route("/api/chat/coverage", _json(200, coverage(start="2026-09-02"))),
    ),
    "coverage_unavailable": (
        "coverage",
        "coverage_not_covered",
        _route("/api/chat/coverage", _json(200, coverage(state="unavailable"))),
    ),
    "coverage_cutoff": (
        "coverage",
        "coverage_invalid",
        _route(
            "/api/chat/coverage",
            _json(200, {**coverage(), "cutoff_date": "2026-09-13"}),
        ),
    ),
    "answer_unconfirmed": (
        "saved_answer",
        "answer_not_complete_or_partial",
        _route(DETAIL, _json(200, {**detail(), "observed_state": "unconfirmed"})),
    ),
    "answer_held": (
        "saved_answer",
        "answer_not_complete_or_partial",
        _route(DETAIL, _json(200, {**detail(), "observed_state": "held"})),
    ),
    "answer_other_request": (
        "saved_answer",
        "request_id_mismatch",
        _route(DETAIL, _json(200, detail(OTHER_RID))),
    ),
    "answer_missing": (
        "saved_answer",
        "request_unavailable",
        _route(
            DETAIL,
            _json(404, {"detail": {"code": "request_unavailable", "message": "x"}}),
        ),
    ),
    "answer_citation_unresolved": (
        "saved_answer",
        "citation_unresolved",
        _detail(_drop_citation),
    ),
    "answer_no_sources": ("saved_answer", "no_source_records", _detail(_no_sources)),
    "answer_no_claims": ("saved_answer", "no_claims", _detail(_no_claims)),
    "answer_label": ("saved_answer", "citation_label_invalid", _detail(_bad_label)),
    "answer_uncited": ("saved_answer", "no_citations", _detail(_uncited)),
    "export_filename": (
        "export",
        "filename_mismatch",
        _route(EXPORT, export_response(filename="answer.html")),
    ),
    "export_title": (
        "export",
        "title_mismatch",
        _route(EXPORT, export_response(title="42 Ask answer")),
    ),
    "export_heading": (
        "export",
        "heading_mismatch",
        _route(EXPORT, export_response(heading="Answer")),
    ),
    "export_refused": (
        "export",
        "answer_not_exportable",
        _route(
            EXPORT,
            _json(409, {"detail": {"code": "answer_not_exportable", "message": "x"}}),
        ),
    ),
    "export_type": (
        "export",
        "content_type_mismatch",
        _route(
            EXPORT,
            {
                **export_response(),
                "headers": {
                    **export_response()["headers"],
                    "Content-Type": "text/plain",
                },
            },
        ),
    ),
    "allowance_ledger": ("allowance", "ledger_invalid", _ledger_inconsistent),
}


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_each_failure_fails_its_own_check_only_and_exits_one(world, case):
    check, reason, edit = FAILURES[case]
    edit(world)
    code, out, _err = run(world)
    assert code == 1
    [line] = receipt_lines(world)
    assert line["overall_status"] == "fail"
    assert line["checks"][check]["status"] == "fail"
    assert line["checks"][check]["reason"] == reason
    others = {
        name: item["status"] for name, item in line["checks"].items() if name != check
    }
    assert set(others.values()) == {"pass"}, others
    assert f"{check}: fail ({reason})" in out


def test_a_mismatched_expected_window_fails_the_coverage_check(world):
    code, _out, _err = run(world, window="2026-08-31/2026-09-13")
    assert code == 1
    result = receipt_lines(world)[0]["checks"]["coverage"]
    assert result["status"] == "fail" and result["reason"] == "window_mismatch"
    assert result["observed"]["start"] == "2026-09-01"


def test_native_reads_without_credentials_are_not_readable_and_exit_two(
    world, monkeypatch
):
    def no_credentials():
        raise RuntimeError("no application default credentials")

    monkeypatch.setattr(native, "_adc_token", no_credentials)
    code, out, _err = run(world)
    assert code == 2
    checks = receipt_lines(world)[0]["checks"]
    assert checks["revision"]["status"] == "not_readable"
    assert checks["allowance"]["status"] == "not_readable"
    assert checks["allowance"]["reason"] == "native_RuntimeError"
    assert checks["coverage"]["status"] == "pass"
    assert receipt_lines(world)[0]["overall_status"] == "not_readable"
    assert "not_readable" in out


def test_a_native_http_denial_is_not_readable_with_the_status_only(world):
    serve = world.cloud.__call__

    def denied(request):
        if request["url"].startswith(native.RUN_ENDPOINT):
            return _json(403, {"error": {"message": "caller lacks run.services.get"}})
        return serve(request)

    world.cloud = denied
    code, _out, _err = run(world)
    assert code == 2
    result = receipt_lines(world)[0]["checks"]["revision"]
    assert result["status"] == "not_readable"
    assert result["reason"] == "native_http_403"


def test_a_staging_outage_is_not_readable(world):
    world.staging.routes["/api/chat/coverage"] = _json(503, {"detail": "unavailable"})
    code, _out, _err = run(world)
    assert code == 2
    result = receipt_lines(world)[0]["checks"]["coverage"]
    assert result["status"] == "not_readable" and result["reason"] == "http_503"


# The access key


def _leak_check(world, out, err):
    assert KEY not in out
    assert KEY not in err
    assert KEY not in world.receipt.read_text()


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_the_key_never_appears_on_http_errors(world, status):
    for path in list(world.staging.routes):
        world.staging.routes[path] = _json(
            status, {"detail": "Missing or invalid passcode"}
        )
    code, out, err = run(world)
    assert code == 2
    _leak_check(world, out, err)


def test_the_key_never_appears_when_the_transport_raises_with_it_in_the_message(world):
    world.staging.raise_error = OSError("connect failed with X-Passcode: " + KEY)
    code, out, err = run(world)
    assert code == 2
    _leak_check(world, out, err)
    reasons = {
        item.get("reason") for item in receipt_lines(world)[0]["checks"].values()
    }
    assert "transport_OSError" in reasons


def test_the_key_never_appears_when_the_server_echoes_it(world):
    echoed = detail()
    echoed["observed_state"] = "held " + KEY
    world.staging.routes[DETAIL] = _json(200, echoed)
    world.staging.routes[EXPORT] = export_response(title=KEY)
    world.staging.routes["/api/chat/coverage"] = _json(
        200, {**coverage(), "state": KEY}
    )
    code, out, err = run(world)
    assert code == 1
    _leak_check(world, out, err)
    assert release_smoke.REDACTED in world.receipt.read_text()


def test_the_key_never_appears_on_a_pass_or_in_any_repr(world):
    code, out, err = run(world)
    assert code == 0
    _leak_check(world, out, err)
    key = release_smoke.read_access_key(world.key_file)
    client = release_smoke.StagingClient(STAGING, key, world.staging)
    assert KEY not in repr(key) and KEY not in str(key) and KEY not in repr(client)
    assert KEY not in repr(vars(client).get("sent"))
    with pytest.raises(TypeError):
        copy.deepcopy(key)


def test_the_key_is_sent_only_in_the_passcode_header_to_the_staging_origin(world):
    run(world)
    for request in world.staging.requests:
        assert request["url"].startswith(STAGING + "/")
        assert KEY not in request["url"]
        assert [name for name, value in request["headers"].items() if value == KEY] == [
            "X-Passcode"
        ]
    assert all(KEY not in json.dumps(item) for item in world.cloud.requests)


def test_a_redirect_is_refused_without_following_it():
    handler = release_smoke._NoRedirect()
    with pytest.raises(release_smoke.Unreadable, match="redirect_refused_http_302"):
        handler.redirect_request(
            None, None, 302, "Found", {}, "https://elsewhere.example/"
        )


# No paid path


def _app_routes():
    source = MAIN.read_text(encoding="utf-8")
    return re.findall(r'@app\.(get|post|put|patch|delete)\(\s*"([^"]+)"', source)


def test_no_paid_path_is_reachable_through_the_staging_client(world):
    key = release_smoke.read_access_key(world.key_file)
    client = release_smoke.StagingClient(STAGING, key, world.staging)
    for method, path in (
        ("POST", "/api/chat/send"),
        ("GET", "/api/chat/send"),
        ("POST", "/api/ask/brief"),
        ("GET", "/api/chat/status"),
        ("POST", "/internal/general-question/execute"),
        ("PUT", "/api/chat/coverage"),
        ("POST", "/api/chat/coverage"),
        ("GET", f"/api/chat/answer/{RID}/export.pdf"),
    ):
        with pytest.raises(release_smoke.RequestRefused):
            client.request(method, path)
    with pytest.raises(release_smoke.RequestRefused):
        client.get("/api/chat/coverage", {"job_id": RID})
    assert world.staging.requests == []


def test_the_allowlist_holds_only_gets_and_matches_no_mutating_app_route():
    assert {method for method, _pattern, _keys in release_smoke.ALLOWED_REQUESTS} == {
        "GET"
    }
    routes = _app_routes()
    assert ("post", "/api/chat/send") in routes
    for verb, path in routes:
        concrete = path.replace("{request_id}", RID)
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            for keys in ((), ("contract_version", "request_id")):
                allowed = release_smoke.request_allowed(method, concrete, keys)
                if verb != "get":
                    assert not allowed, (verb, path)
    allowed_routes = {
        path
        for verb, path in routes
        if verb == "get"
        and any(
            release_smoke.request_allowed(
                "GET", path.replace("{request_id}", RID), keys
            )
            for keys in ((), ("contract_version", "request_id"))
        )
    }
    assert allowed_routes == {
        "/api/chat/coverage",
        "/api/internal/v2/fieldwork/question",
        "/api/chat/answer/{request_id}/export.html",
    }


def test_native_reads_refuse_every_method_but_get(world):
    clients = release_smoke.native_clients(world.cloud)
    with pytest.raises(release_smoke.RequestRefused):
        clients["run"].adapter.perform(
            "read_operation", name=SERVICE_API + "/operations/x", timeout_seconds=1
        )
    with pytest.raises(release_smoke.RequestRefused):
        clients["objects"].create("x.json", b"{}", if_generation_match=0)
    assert world.cloud.requests == []
    assert set(clients) == {"run", "objects"}


def test_the_export_and_reload_reads_reserve_no_allowance_in_the_app_code():
    source = MAIN.read_text(encoding="utf-8")

    def body(name):
        start = source.index(f"def {name}(")
        end = source.index("\n@app.", start)
        return source[start:end]

    for name in ("_answer_export_page", "api_fieldwork_question"):
        text = body(name)
        assert "read_question_observation" in text
        for paid in ("start_route", "_check_rate_limit", "reserve", "run_chat"):
            assert paid not in text, (name, paid)
    for name in ("api_chat_answer_export_html", "api_chat_coverage"):
        assert "start_route" not in body(name)


# The receipt


def test_the_receipt_only_grows_and_earlier_lines_stay_byte_identical(world):
    world.receipt.write_bytes(b'{"earlier":"line"}\n')
    before = world.receipt.read_bytes()
    assert run(world)[0] == 0
    first = world.receipt.read_bytes()
    assert first.startswith(before)
    world.staging.routes[EXPORT] = export_response(title="other")
    assert run(world)[0] == 1
    second = world.receipt.read_bytes()
    assert second.startswith(first)
    lines = second.decode().splitlines()
    assert len(lines) == 3
    assert [json.loads(line).get("overall_status") for line in lines] == [
        None,
        "pass",
        "fail",
    ]


def test_a_symlinked_receipt_is_refused_before_any_request(world):
    target = world.tmp / "elsewhere.jsonl"
    target.write_bytes(b"kept\n")
    link = world.tmp / "link.jsonl"
    link.symlink_to(target)
    code, _out, err = run(world, **{"--receipt": str(link)})
    assert code == 2
    assert "receipt_symlink" in err
    assert target.read_bytes() == b"kept\n"
    assert world.staging.requests == [] and world.cloud.requests == []


def test_an_unterminated_receipt_is_refused_and_left_as_it_was(world):
    world.receipt.write_bytes(b'{"partial":')
    code, _out, err = run(world)
    assert code == 2
    assert "receipt_unterminated" in err
    assert world.receipt.read_bytes() == b'{"partial":'


def test_a_directory_receipt_is_refused(world):
    code, _out, err = run(world, **{"--receipt": str(world.tmp)})
    assert code == 2 and "receipt_not_file" in err


# Refused inputs


def _invalid_record(world):
    world.record.write_text(json.dumps({"schema_version": "42_staging_release_v2"}))


def _missing_key(world):
    world.key_file.unlink()


def _empty_key(world):
    world.key_file.write_text("\n")


REFUSALS = {
    "release_record_invalid": (_invalid_record, {}),
    "access_key_unreadable": (_missing_key, {}),
    "access_key_invalid": (_empty_key, {}),
    "staging_url_invalid": (
        lambda world: None,
        {"--staging-url": "http://staging.example"},
    ),
    "expected_window_invalid": (
        lambda world: None,
        {"--expected-window": "2026-09-14/2026-09-01"},
    ),
    "request_id_invalid": (lambda world: None, {"--request-id": "not-a-uuid"}),
}


@pytest.mark.parametrize("reason", sorted(REFUSALS))
def test_a_refused_input_exits_two_and_records_the_refusal_without_a_request(
    world, reason
):
    edit, overrides = REFUSALS[reason]
    edit(world)
    code, out, _err = run(world, **overrides)
    assert code == 2
    [line] = receipt_lines(world)
    assert line["overall_status"] == "refused" and line["reason"] == reason
    assert "checks" not in line
    assert world.staging.requests == [] and world.cloud.requests == []
    assert reason in out


# Escaped forms of the key

HARD_KEY = "ab\"c\\d'e%f"


def _escaped_forms(value):
    return {
        value,
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        repr(value)[1:-1],
        ascii(value)[1:-1],
    }


def test_a_key_with_quotes_a_backslash_and_non_ascii_never_appears_in_any_form(world):
    world.key_file.write_text(HARD_KEY + "\n", encoding="utf-8")
    echoed = detail()
    echoed["observed_state"] = "held " + HARD_KEY
    world.staging.routes[DETAIL] = _json(200, echoed)
    world.staging.routes[EXPORT] = export_response(title="t " + HARD_KEY)
    world.staging.routes["/api/chat/coverage"] = _json(
        200, {**coverage(), "state": HARD_KEY, HARD_KEY: "key as a field name"}
    )
    code, out, err = run(world)
    assert code == 1
    receipt = world.receipt.read_text(encoding="utf-8")
    for text in (out, err, receipt):
        for form in _escaped_forms(HARD_KEY):
            assert form not in text, form
    assert release_smoke.REDACTED in receipt and release_smoke.REDACTED in out
    assert all(
        item["headers"]["X-Passcode"] == HARD_KEY for item in world.staging.requests
    )


@pytest.mark.parametrize(
    "value",
    [
        "ab\tc",
        "ab\x00c",
        "ab\u200bc",
        "ab\x85c",
        "ab c",
        "ab\u2028c",
        "ab\u00e9c",
        "ab\x7fc",
    ],
)
def test_a_key_outside_printable_ascii_is_refused(world, value):
    world.key_file.write_text(value, encoding="utf-8")
    code, out, _err = run(world)
    assert code == 2
    assert receipt_lines(world)[0]["reason"] == "access_key_invalid"
    assert world.staging.requests == []


# Malformed server shapes


def _claims_edit(edit):
    def apply(value):
        edit(value["response"]["intelligence"])

    return _detail(apply)


def _revision_edit(edit):
    def apply(world):
        edit(world.cloud.revision)

    return apply


MALFORMED = {
    "receipt_ids_int": (
        "saved_answer",
        "claim_references_invalid",
        _claims_edit(lambda reply: reply["claims"][0].update(receipt_ids=7)),
    ),
    "receipt_ids_dicts": (
        "saved_answer",
        "claim_references_invalid",
        _claims_edit(lambda reply: reply["claims"][0].update(receipt_ids=[{"a": 1}])),
    ),
    "reading_ids_lists": (
        "saved_answer",
        "claim_references_invalid",
        _claims_edit(lambda reply: reply["claims"][1].update(reading_ids=[["rd-1"]])),
    ),
    "reading_sources_dict": (
        "saved_answer",
        "reading_references_invalid",
        _claims_edit(
            lambda reply: reply["readings"][0].update(source_receipt_ids={"rc-2": 1})
        ),
    ),
    "observed_state_list": (
        "saved_answer",
        "detail_invalid",
        _route(DETAIL, _json(200, {**detail(), "observed_state": ["complete"]})),
    ),
    "revision_env_unnamed": (
        "revision",
        "revision_env_invalid",
        _revision_edit(
            lambda revision: revision["containers"][0]["env"].append({"value": "x"})
        ),
    ),
    "revision_conditions_int": (
        "revision",
        "revision_conditions_invalid",
        _revision_edit(lambda revision: revision.update(conditions=3)),
    ),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_server_shape_fails_by_name_and_still_writes_a_receipt(world, case):
    check, reason, edit = MALFORMED[case]
    edit(world)
    code, out, err = run(world)
    assert code == 1, err
    [line] = receipt_lines(world)
    assert line["checks"][check] == {**line["checks"][check], "status": "fail"}
    assert line["checks"][check]["reason"] == reason
    assert "Traceback" not in out + err


def test_a_revision_that_is_not_an_object_fails_by_name(world):
    serve = world.cloud.__call__
    name = world.cloud.revision["name"]

    def listed(request):
        if request["url"] == native.RUN_ENDPOINT + name:
            return _json(200, ["not", "an", "object"])
        return serve(request)

    world.cloud = listed
    assert run(world)[0] == 1
    result = receipt_lines(world)[0]["checks"]["revision"]
    assert (result["status"], result["reason"]) == ("fail", "revision_invalid")


def test_an_unexpected_exception_in_a_check_is_not_readable_by_class_name(
    world, monkeypatch
):
    def broken(client, request_id):
        raise KeyError(KEY)

    monkeypatch.setattr(release_smoke, "check_export", broken)
    code, out, err = run(world)
    assert code == 2
    [line] = receipt_lines(world)
    assert line["checks"]["export"] == {
        "status": "not_readable",
        "observed": {},
        "reason": "check_error_KeyError",
    }
    assert line["checks"]["coverage"]["status"] == "pass"
    _leak_check(world, out, err)


def test_a_receipt_swapped_for_another_file_is_refused(world, monkeypatch):
    world.receipt.write_bytes(b'{"earlier":"line"}\n')
    other = world.tmp / "other.jsonl"
    other.write_bytes(b"other\n")
    real_open = release_smoke.os.open

    def swapping_open(path, flags, *mode):
        if flags & release_smoke.os.O_RDONLY == 0 and not flags & (
            release_smoke.os.O_WRONLY | release_smoke.os.O_RDWR
        ):
            return real_open(other, flags, *mode)
        return real_open(path, flags, *mode)

    monkeypatch.setattr(release_smoke.os, "open", swapping_open)
    code, _out, err = run(world)
    assert code == 2 and "receipt_replaced" in err
    assert world.receipt.read_bytes() == b'{"earlier":"line"}\n'
    assert other.read_bytes() == b"other\n"


def test_a_revision_the_native_reader_cannot_parse_is_not_readable(world):
    world.cloud.revision["containers"][0]["env"] = 5
    code, _out, err = run(world)
    assert code == 2 and "Traceback" not in err
    result = receipt_lines(world)[0]["checks"]["revision"]
    assert result["status"] == "not_readable"
    assert result["reason"].startswith("native_")


# Scrubbing touches only what the servers sent


@pytest.mark.parametrize("short", ["s", "2026"])
def test_a_short_key_leaves_the_record_fields_timestamps_and_exit_codes_intact(
    world, short
):
    world.key_file.write_text(short, encoding="utf-8")
    code, out, err = run(world)
    assert code == 0, out + err
    [line] = receipt_lines(world)
    assert line["checked_at"] == "2026-09-25T06:00:00Z"
    assert line["overall_status"] == "pass"
    assert line["release_commit"] == LP_COMMIT
    assert set(line["checks"]) == set(release_smoke.CHECKS)
    for result in line["checks"].values():
        assert result["status"] == "pass"
        assert {"status", "observed"} <= set(result)
    assert set(line["checks"]["coverage"]["observed"]) == {
        "http_status",
        "state",
        "start",
        "end",
        "cutoff_date",
    }
    assert "release smoke pass at 2026-09-25T06:00:00Z" in out


def test_a_short_key_still_fails_a_failing_run_with_exit_one(world):
    world.key_file.write_text("s", encoding="utf-8")
    world.staging.routes[EXPORT] = export_response(title="other")
    code, _out, _err = run(world)
    assert code == 1
    line = receipt_lines(world)[0]
    assert line["overall_status"] == "fail"
    assert line["checks"]["export"]["reason"] == "title_mismatch"


def test_a_numeric_echo_of_the_key_is_redacted():
    key = release_smoke.AccessKey("4242")
    assert key.scrub_value({"n": 4242, "f": 14242.5, "ok": 7, "b": True}) == {
        "n": release_smoke.REDACTED,
        "f": release_smoke.REDACTED,
        "ok": 7,
        "b": True,
    }


def test_an_integer_status_holding_the_key_is_redacted_in_the_receipt(world):
    world.key_file.write_text("503", encoding="utf-8")
    world.staging.routes["/api/chat/coverage"] = _json(503, {"detail": "x"})
    code, out, err = run(world)
    assert code == 2
    result = receipt_lines(world)[0]["checks"]["coverage"]
    assert result["observed"]["http_status"] == release_smoke.REDACTED
    assert result["reason"] == "http_503"


ENCODED_KEY = "ab%22c%5Cd%27e%25f"


@pytest.mark.parametrize(
    "echo", [HARD_KEY.upper(), ENCODED_KEY, ENCODED_KEY.upper(), ENCODED_KEY.lower()]
)
def test_percent_encoded_and_upper_cased_echoes_are_redacted(world, echo):
    assert urllib.parse.quote(HARD_KEY, safe="") == ENCODED_KEY
    world.key_file.write_text(HARD_KEY, encoding="utf-8")
    world.staging.routes[EXPORT] = export_response(title="t " + echo)
    code, out, err = run(world)
    assert code == 1
    receipt = world.receipt.read_text()
    title = json.loads(receipt)["checks"]["export"]["observed"]["title"]
    assert title == "t " + release_smoke.REDACTED
    for text in (out, err, receipt):
        for form in (HARD_KEY, ENCODED_KEY):
            assert form.lower() not in text.lower()


def test_a_receipt_write_that_fails_exits_two(world, monkeypatch):
    import errno

    def full(descriptor, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(release_smoke.os, "write", full)
    code, _out, err = run(world)
    assert code == 2 and "receipt_unwritable" in err


def test_a_short_receipt_write_exits_two(world, monkeypatch):
    real_write = release_smoke.os.write

    def short(descriptor, data):
        return real_write(descriptor, data[:5])

    monkeypatch.setattr(release_smoke.os, "write", short)
    code, _out, err = run(world)
    assert code == 2 and "receipt_unwritable" in err


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_in_server_json_fail_by_name(world, constant):
    body = json.dumps(detail()).replace(
        '"reserved_microusd": 100000', ('"reserved_microusd": ' + constant)
    )
    world.staging.routes[DETAIL] = {
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "body": body.encode(),
    }
    world.staging.routes["/api/chat/coverage"] = {
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(coverage())
        .replace("}", ', "x": ' + constant + "}", 1)
        .encode(),
    }
    code, _out, _err = run(world)
    assert code == 1
    checks = receipt_lines(world)[0]["checks"]
    assert checks["saved_answer"]["reason"] == "response_non_finite_number"
    assert checks["coverage"]["reason"] == "response_non_finite_number"


def test_an_unknown_server_refusal_code_never_becomes_a_reason(world):
    world.staging.routes[EXPORT] = _json(
        409, {"detail": {"code": "made_up_code", "message": "x"}}
    )
    code, _out, _err = run(world)
    assert code == 1
    assert receipt_lines(world)[0]["checks"]["export"]["reason"] == "http_409"


@pytest.mark.parametrize(
    "field, value",
    [("state", {KEY: 1}), ("cutoff_date", {KEY.upper(): "2026-09-14"})],
)
def test_a_key_in_a_coverage_mapping_never_reaches_the_receipt(world, field, value):
    body = coverage()
    body[field] = value
    world.staging.routes["/api/chat/coverage"] = _json(200, body)
    code, out, err = run(world)
    assert code == 1
    text = world.receipt.read_text(encoding="utf-8")
    assert KEY.lower() not in (text + out + err).lower()
    assert receipt_lines(world)[0]["checks"]["coverage"]["observed"][field] is None


def test_an_unreadable_receipt_path_is_refused_with_exit_two(world):
    long_name = str(world.tmp / ("r" * 5000))
    code, _out, err = run(world, **{"--receipt": long_name})
    assert code == 2 and "receipt_unwritable" in err
    assert world.staging.requests == [] and world.cloud.requests == []


def _traffic_percent(value):
    def edit(world):
        world.cloud.service["trafficStatuses"].append(
            {"revision": "other-revision", "percent": value, "tag": None}
        )

    return edit


def _traffic_tag(value):
    def edit(world):
        world.cloud.service["trafficStatuses"][0]["tag"] = value

    return edit


@pytest.mark.parametrize(
    "edit",
    [
        _traffic_percent(float("nan")),
        _traffic_percent(float("inf")),
        _traffic_percent(float("-inf")),
        _traffic_tag(float("nan")),
        _traffic_tag(float("inf")),
    ],
    ids=["percent_nan", "percent_inf", "percent_minus_inf", "tag_nan", "tag_inf"],
)
def test_a_non_finite_native_value_is_refused_and_writes_no_invalid_line(world, edit):
    # The native Cloud Run read parses NaN and Infinity as floats; the receipt must
    # never carry them, since strict JSON has no such numbers.
    world.receipt.write_bytes(b'{"earlier":"line"}\n')
    before = world.receipt.read_bytes()
    edit(world)
    code, out, err = run(world)
    assert code == 2
    assert "receipt_non_finite_value" in err and "Traceback" not in err
    assert out == ""
    assert world.receipt.read_bytes() == before
    for line in world.receipt.read_text(encoding="utf-8").splitlines():
        json.loads(line, parse_constant=lambda constant: pytest.fail(constant))


def test_a_non_finite_native_value_leaves_no_receipt_file_behind(world):
    _traffic_percent(float("nan"))(world)
    code, _out, err = run(world)
    assert code == 2 and "receipt_non_finite_value" in err
    assert not world.receipt.exists()
