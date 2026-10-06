"""The Ask page names the dates it can answer, read from the worker's own registry."""

import asyncio
import copy
import importlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from tests.unit.test_question_worker_result import EXPECTED

REPOSITORY = Path(__file__).resolve().parents[3]
REGISTRY = ("configs", "open_intelligence", "protected_context_registry_v1.json")
LANES = ("event_ledger", "seed_graph", "seed_candidates", "enriched_content", "raw_content")


def routes():
    return importlib.import_module("src.api.general_question_routes")


def entry(cutoff, *, pinned):
    compact = cutoff.replace("-", "")
    next_day = date.fromordinal(date.fromisoformat(cutoff).toordinal() + 1).isoformat()
    return {
        "consumption_id": "exc_" + "a" * 64 if pinned else None,
        "cutoff_date": cutoff,
        "manifest_sha256": "b" * 64 if pinned else None,
        "market_scope": ["ke", "ng", "za"],
        "profile_id": f"protected_context_{compact}_v1",
        "result_digest": "c" * 64 if pinned else None,
        "result_id": "exr_" + "d" * 64 if pinned else None,
        "snapshot_tables": [
            f"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_{compact}_{lane}"
            for lane in LANES
        ],
        "source_as_of": f"{next_day}T00:00:00+00:00",
    }


def bundle(tmp_path, entries, *, raw=None):
    path = tmp_path.joinpath(*REGISTRY)
    path.parent.mkdir(parents=True)
    value = {
        "contract_version": "protected_context_registry_v1",
        "entries": entries,
        "registry_id": "protected_context_registry_v1",
    }
    path.write_bytes(
        raw
        if raw is not None
        else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    )
    return tmp_path


def test_the_latest_pinned_closed_profile_bounds_a_fourteen_day_window(tmp_path):
    root = bundle(tmp_path, [entry("2026-09-07", pinned=True), entry("2026-09-08", pinned=False)])
    assert routes().question_coverage(root, today=date(2026, 9, 23)) == {
        "contract_version": "general_question_coverage_v1",
        "state": "covered",
        "window": {"start": "2026-08-25", "end": "2026-09-07"},
        "cutoff_date": "2026-09-07",
    }


def test_a_newer_pinned_profile_moves_the_window(tmp_path):
    root = bundle(tmp_path, [entry("2026-09-07", pinned=True), entry("2026-09-08", pinned=True)])
    value = routes().question_coverage(root, today=date(2026, 9, 23))
    assert value["window"] == {"start": "2026-08-26", "end": "2026-09-08"}
    assert value["cutoff_date"] == "2026-09-08"


def test_a_profile_is_served_only_once_its_cutoff_day_has_closed(tmp_path):
    root = bundle(tmp_path, [entry("2026-09-07", pinned=True), entry("2026-09-08", pinned=True)])
    assert routes().question_coverage(root, today=date(2026, 9, 8))["cutoff_date"] == "2026-09-07"
    assert routes().question_coverage(root, today=date(2026, 9, 7)) == {
        "contract_version": "general_question_coverage_v1",
        "state": "unavailable",
        "window": None,
        "cutoff_date": None,
    }


def test_the_registry_the_worker_bundle_carries_parses():
    value = routes().question_coverage(REPOSITORY / "engine", today=date(2026, 9, 23))
    assert value["state"] == "covered"
    assert value["window"]["end"] == value["cutoff_date"]


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"not json",
        b'{"contract_version":"protected_context_registry_v1","entries":[],"registry_id":"protected_context_registry_v1"}',
        b'{"registry_id":"protected_context_registry_v1","contract_version":"protected_context_registry_v1","entries":[]}',
        b'{"contract_version":"protected_context_registry_v2","entries":[{}],"registry_id":"protected_context_registry_v2"}',
    ],
)
def test_an_unreadable_registry_is_refused_rather_than_guessed(tmp_path, raw):
    root = bundle(tmp_path, [], raw=raw)
    with pytest.raises(ValueError):
        routes().question_coverage(root, today=date(2026, 9, 23))


@pytest.mark.parametrize(
    "change",
    [
        {"cutoff_date": "2026-9-07"},
        {"profile_id": "protected_context_20260906_v1"},
        {"result_id": None},
        {"market_scope": ["za", "ke"]},
        {"market_scope": [1]},
        {"market_scope": ["za", 1]},
        {"source_as_of": "2026-09-09T00:00:00+00:00"},
        {"source_as_of": "2026-09-08T00:00:00Z"},
        {"snapshot_tables": ["x"]},
        {"snapshot_tables": []},
    ],
)
def test_a_malformed_entry_is_refused(tmp_path, change):
    root = bundle(tmp_path, [entry("2026-09-07", pinned=True) | change])
    with pytest.raises(ValueError):
        routes().question_coverage(root, today=date(2026, 9, 23))


def test_unordered_cutoffs_are_refused(tmp_path):
    root = bundle(tmp_path, [entry("2026-09-08", pinned=True), entry("2026-09-07", pinned=True)])
    with pytest.raises(ValueError):
        routes().question_coverage(root, today=date(2026, 9, 23))


def canonical(entries, version="protected_context_registry_v1"):
    return {"contract_version": version, "entries": entries, "registry_id": version}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def far_entry():
    return {
        **entry("2026-09-07", pinned=True),
        "cutoff_date": "9999-12-31",
        "profile_id": "protected_context_99991231_v1",
        "snapshot_tables": [
            f"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_99991231_{lane}"
            for lane in LANES
        ],
    }


# Registries the engine's own loader refuses before it reads a single entry,
# or while it does, each with the code the refusal is logged under.
RAW_REFUSALS = {
    "size": (b" " * (1024 * 1024 + 1), "registry too large"),
    "noncanonical": (
        json.dumps(canonical([entry("2026-09-07", pinned=True)]), indent=1).encode(),
        "noncanonical registry",
    ),
    "duplicate_key": (
        b'{"contract_version":"protected_context_registry_v1","contract_version":"protected_context_registry_v1","entries":[],"registry_id":"protected_context_registry_v1"}',
        "duplicate key",
    ),
    "float": (
        b'{"contract_version":"protected_context_registry_v1","entries":[1.5],"registry_id":"protected_context_registry_v1"}',
        "noninteger number",
    ),
    "constant": (
        b'{"contract_version":"protected_context_registry_v1","entries":[NaN],"registry_id":"protected_context_registry_v1"}',
        "noninteger number",
    ),
    "version": (
        encoded(canonical([entry("2026-09-07", pinned=True)], "protected_context_registry_v2")),
        "registry version or id",
    ),
    "far_cutoff": (encoded(canonical([entry("2026-09-07", pinned=True), far_entry()])), "OverflowError"),
    "deep": (b"[" * 200000 + b"]" * 200000, "RecursionError"),
}


# One registry per reason the engine loader refuses by name, so dropping a reason
# from the route's list turns the app check and the engine parity red alike.
REASONS = (
    "authority pins must be complete or all null",
    "cutoff date",
    "duplicate key",
    "duplicate or unordered cutoffs",
    "entries",
    "entry fields",
    "market scope",
    "noncanonical registry",
    "noninteger number",
    "profile id",
    "registry fields",
    "registry too large",
    "registry version or id",
    "snapshot tables",
    "source as of",
)


def reason_registries():
    pinned = entry("2026-09-07", pinned=True)
    changed = {
        "authority pins must be complete or all null": {"result_id": None},
        "cutoff date": {"cutoff_date": 20260907},
        "entry fields": {"note": "x"},
        "market scope": {"market_scope": ["za", "ke"]},
        "profile id": {"profile_id": "protected_context_20260906_v1"},
        "snapshot tables": {"snapshot_tables": ["x"]},
        "source as of": {"source_as_of": "2026-09-09T00:00:00+00:00"},
    }
    raw = {
        reason: encoded(canonical([pinned | change]))
        for reason, change in changed.items()
    }
    raw["duplicate or unordered cutoffs"] = encoded(
        canonical([entry("2026-09-08", pinned=True), pinned])
    )
    raw["entries"] = encoded(canonical([]))
    raw["registry fields"] = b"{}"
    for name in ("duplicate_key", "noncanonical", "float", "size", "version"):
        value, reason = RAW_REFUSALS[name]
        raw[reason] = value
    return raw


def test_every_named_reason_has_a_registry_that_the_route_refuses_by_it(tmp_path):
    registries = reason_registries()
    assert sorted(registries) == sorted(REASONS)
    for reason, raw in registries.items():
        root = bundle(tmp_path / reason.replace(" ", "_"), [], raw=raw)
        with pytest.raises(ValueError) as raised:
            routes().question_coverage(root, today=date(2026, 9, 23))
        assert routes().coverage_refusal_code(raised.value) == reason


PAYLOAD = "<script>payload</script>"


def test_a_refusal_that_quotes_what_it_read_is_logged_by_its_class_alone(
    tmp_path, caplog
):
    raw = encoded(
        canonical(
            [entry("2026-09-07", pinned=True) | {"cutoff_date": "2026-09-07" + PAYLOAD}]
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                general_question_routes=service(bundle(tmp_path, [], raw=raw))
            )
        )
    )
    with caplog.at_level("WARNING", logger="listening_post.question_worker"):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(routes().coverage_route(request))
    assert raised.value.status_code == 503
    assert raised.value.detail == "Question coverage is unavailable"
    assert [record.getMessage() for record in caplog.records] == [
        "question_coverage_unavailable reason=ValueError"
    ]
    assert all(
        record.exc_info is None and record.exc_text is None for record in caplog.records
    )
    assert "payload" not in caplog.text


@pytest.mark.parametrize("name", sorted(RAW_REFUSALS))
def test_each_refusal_names_what_the_engine_would_refuse(tmp_path, name):
    raw, expected = RAW_REFUSALS[name]
    root = bundle(tmp_path, [], raw=raw)
    with pytest.raises(ValueError) as raised:
        routes().question_coverage(root, today=date(2026, 9, 23))
    assert str(raised.value) == "coverage_registry_invalid"
    assert routes().coverage_refusal_code(raised.value) == expected


PARITY = """
import json, sys
from datetime import date, timedelta
from pathlib import Path
import src.analysis.open_intelligence.protected_context_registry as registry
import src.analysis.open_intelligence.general_question_context_admission as admission
reasons = set(json.loads(sys.argv[1]))
def code(error):
    cause = error.__cause__ if error.__cause__ is not None else error
    if type(cause) is ValueError and cause.args and cause.args[0] in reasons:
        return cause.args[0]
    return type(cause).__name__
results = []
for path, today in json.loads(sys.stdin.read()):
    registry.REGISTRY_PATH = Path(path)
    registry._cache = None
    try:
        hint = admission.source_window_hint(today + "T10:00:00+00:00")
    except ValueError as error:
        assert str(error) == "protected_context_registry_invalid", error
        results.append(["refused", code(error)])
        continue
    if hint is None:
        results.append(None)
        continue
    end = date.fromisoformat(hint["cutoff_date"])
    results.append([(end - timedelta(days=13)).isoformat(), end.isoformat()])
print(json.dumps(results))
"""


def registry_variants():
    real = json.loads(REPOSITORY.joinpath("engine", *REGISTRY).read_bytes())
    both = copy.deepcopy(real)
    both["entries"][1].update(
        {key: both["entries"][0][key] for key in ("consumption_id", "manifest_sha256", "result_digest", "result_id")}
    )
    variants = {"real": encoded(real), "both_pinned": encoded(both)}
    for name, change in {
        "bad_source_as_of": {"source_as_of": "2026-09-09T00:00:00+00:00"},
        "bad_tables": {"snapshot_tables": ["x"]},
        "bad_markets": {"market_scope": ["za", "ke"]},
        "bad_profile": {"profile_id": "protected_context_20260906_v1"},
        "half_pinned": {"result_id": None},
        "extra_field": {"note": "x"},
    }.items():
        value = copy.deepcopy(real)
        value["entries"][0].update(change)
        variants[name] = encoded(value)
    unordered = copy.deepcopy(real)
    unordered["entries"].reverse()
    variants["unordered"] = encoded(unordered)
    variants["real_v2"] = encoded({**real, "contract_version": "protected_context_registry_v2", "registry_id": "protected_context_registry_v2"})
    variants["real_indented"] = json.dumps(real, indent=1, sort_keys=True).encode()
    variants["real_duplicate_key"] = encoded(real).replace(b'{"contract_version":', b'{"registry_id":"protected_context_registry_v1","contract_version":', 1)
    variants["real_float"] = encoded(real).replace(b'"entries":[', b'"entries":[1.0,', 1)
    for name, (raw, _) in RAW_REFUSALS.items():
        variants["raw_" + name] = raw
    for reason, raw in reason_registries().items():
        variants["reason_" + reason.replace(" ", "_")] = raw
    variants["payload_cutoff"] = encoded(
        canonical(
            [entry("2026-09-07", pinned=True) | {"cutoff_date": "2026-09-07" + PAYLOAD}]
        )
    )
    return variants


def test_the_route_serves_exactly_the_window_the_engine_would_select(tmp_path):
    cases, expected_app = [], []
    for name, raw in registry_variants().items():
        root = bundle(tmp_path / name, [], raw=raw)
        for today in (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 23)):
            cases.append([str(root.joinpath(*REGISTRY)), today.isoformat()])
            try:
                served = routes().question_coverage(root, today=today)
            except ValueError as error:
                expected_app.append((name, today.isoformat(), ["refused", routes().coverage_refusal_code(error)]))
                continue
            window = served["window"]
            expected_app.append((name, today.isoformat(), None if window is None else [window["start"], window["end"]]))
    completed = subprocess.run(
        [sys.executable, "-c", PARITY, json.dumps(sorted(REASONS))],
        input=json.dumps(cases),
        cwd=REPOSITORY / "engine",
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    engine = json.loads(completed.stdout.strip().splitlines()[-1])
    assert [(name, today, result) for (name, today, _), result in zip(expected_app, engine, strict=True)] == expected_app
    assert ("real", "2026-09-23", ["2026-08-25", "2026-09-07"]) in expected_app
    assert ("bad_source_as_of", "2026-09-23", ["refused", "source as of"]) in expected_app
    assert ("real_v2", "2026-09-23", ["refused", "registry version or id"]) in expected_app
    assert ("payload_cutoff", "2026-09-23", ["refused", "ValueError"]) in expected_app
    for reason in REASONS:
        name = "reason_" + reason.replace(" ", "_")
        assert (name, "2026-09-23", ["refused", reason]) in expected_app


def service(root):
    worker = SimpleNamespace(bundle_root=root)
    return routes().GeneralQuestionRoutes(
        worker,
        None,
        policy_digest=EXPECTED["policy_digest"],
        deployment_digest=EXPECTED["deployment_digest"],
    )


def test_the_route_reads_the_configured_worker_bundle(tmp_path, monkeypatch):
    root = bundle(tmp_path, [entry("2026-09-07", pinned=True)])
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(general_question_routes=service(root)))
    )
    monkeypatch.setattr(routes(), "_coverage_today", lambda: date(2026, 9, 23))
    assert asyncio.run(routes().coverage_route(request))["window"] == {
        "start": "2026-08-25",
        "end": "2026-09-07",
    }


def test_the_route_is_unavailable_without_a_worker_or_a_readable_registry(tmp_path):
    missing = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(routes().coverage_route(missing))
    assert raised.value.status_code == 503
    assert raised.value.detail == "General question worker is not configured"
    broken = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(general_question_routes=service(bundle(tmp_path, [], raw=b"{}")))
        )
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(routes().coverage_route(broken))
    assert raised.value.status_code == 503
    assert raised.value.detail == "Question coverage is unavailable"


@pytest.mark.parametrize("name", ["far_cutoff", "deep", "version"])
def test_every_refused_registry_is_a_503_logged_by_its_code_alone(tmp_path, name, caplog):
    raw, expected = RAW_REFUSALS[name]
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(general_question_routes=service(bundle(tmp_path, [], raw=raw))))
    )
    with caplog.at_level("WARNING", logger="listening_post.question_worker"):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(routes().coverage_route(request))
    assert raised.value.status_code == 503
    assert raised.value.detail == "Question coverage is unavailable"
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [f"question_coverage_unavailable reason={expected}"]


def test_the_http_route_is_gated_and_serves_the_coverage(tmp_path, monkeypatch):
    main = importlib.import_module("src.api.main")
    monkeypatch.setenv("UI_PASSCODE", "synthetic-test-passcode")
    root = bundle(tmp_path, [entry("2026-09-07", pinned=True)])
    monkeypatch.setattr(main.app.state, "general_question_routes", service(root), raising=False)
    monkeypatch.setattr(routes(), "_coverage_today", lambda: date(2026, 9, 23))
    client = TestClient(main.app)
    assert client.get("/api/chat/coverage").status_code == 401
    response = client.get("/api/chat/coverage", headers={"X-Passcode": "synthetic-test-passcode"})
    assert response.status_code == 200
    assert response.json()["window"] == {"start": "2026-08-25", "end": "2026-09-07"}


# A bridge capture pinned beside the v1 profiles: the worker answers on it only while its
# bridge reads switch is on, so the page names its window only then.

BRIDGE_LANES = (
    "enriched_content",
    "event_ledger",
    "raw_content",
    "seed_candidates",
    "seed_graph",
    "trend_analysis",
    "trend_scores",
)
V2_LANES = ("enriched_content", "event_ledger", "raw_content", "seed_candidates", "seed_graph")


def bridge_entry(cutoff, *, derivation=False, **changes):
    compact = cutoff.replace("-", "")
    next_day = date.fromordinal(date.fromisoformat(cutoff).toordinal() + 1).isoformat()
    value = {
        "consumption_id": "exc_" + "e" * 64,
        "cutoff_date": cutoff,
        "manifest_sha256": "f" * 64,
        "market_scope": ["ke", "ng", "za"],
        "profile_id": f"staging_bridge_v3_{compact}",
        "result_contract_version": "open_intelligence_execution_result_v2",
        "result_digest": "1" * 64,
        "result_id": "exr_" + "2" * 64,
        "snapshot_tables": [
            f"ogilvy-trends-v2.trends_v2_staging.staging_bridge_v3_{compact}_{lane}"
            for lane in BRIDGE_LANES
        ],
        "source_as_of": f"{next_day}T00:00:00+00:00",
    }
    if derivation:
        value.update(
            derivation_id="exd_" + "3" * 64,
            result_contract_version="open_intelligence_execution_result_v3",
            result_id="exr_" + value["result_digest"],
        )
    value.update(changes)
    return value


def v2_entry(cutoff):
    compact = cutoff.replace("-", "")
    return entry(cutoff, pinned=True) | {
        "profile_id": f"protected_context_{compact}_v2",
        "result_contract_version": "open_intelligence_execution_result_v2",
        "snapshot_tables": [
            f"ogilvy-trends-v2.intelligence_42_sources_staging.staging_source_{compact}_{lane}"
            for lane in V2_LANES
        ],
    }


@pytest.fixture
def bridge_reads(monkeypatch):
    monkeypatch.setenv("GENERAL_QUESTION_BRIDGE_READS", "enabled")


def test_a_pinned_bridge_capture_moves_the_window_while_bridge_reads_are_on(
    tmp_path, bridge_reads
):
    root = bundle(tmp_path, [entry("2026-09-07", pinned=True), bridge_entry("2026-09-23")])
    assert routes().question_coverage(root, today=date(2026, 9, 24)) == {
        "contract_version": "general_question_coverage_v1",
        "state": "covered",
        "window": {"start": "2026-09-10", "end": "2026-09-23"},
        "cutoff_date": "2026-09-23",
    }


def test_a_daily_chain_bridge_capture_is_served_the_same_way(tmp_path, bridge_reads):
    root = bundle(
        tmp_path, [entry("2026-09-07", pinned=True), bridge_entry("2026-09-23", derivation=True)]
    )
    assert routes().question_coverage(root, today=date(2026, 9, 24))["cutoff_date"] == "2026-09-23"


@pytest.mark.parametrize("switch", [None, "", "on", "Enabled"])
def test_without_bridge_reads_a_bridge_capture_is_listed_but_not_served(
    tmp_path, monkeypatch, switch
):
    if switch is None:
        monkeypatch.delenv("GENERAL_QUESTION_BRIDGE_READS", raising=False)
    else:
        monkeypatch.setenv("GENERAL_QUESTION_BRIDGE_READS", switch)
    root = bundle(tmp_path, [entry("2026-09-07", pinned=True), bridge_entry("2026-09-23")])
    value = routes().question_coverage(root, today=date(2026, 9, 24))
    assert value["cutoff_date"] == "2026-09-07"
    assert value["window"] == {"start": "2026-08-25", "end": "2026-09-07"}


def test_a_bridge_capture_is_served_only_once_its_day_has_closed(tmp_path, bridge_reads):
    root = bundle(tmp_path, [entry("2026-09-07", pinned=True), bridge_entry("2026-09-23")])
    assert routes().question_coverage(root, today=date(2026, 9, 23))["cutoff_date"] == "2026-09-07"


def test_a_v2_profile_is_valid_but_never_a_ceiling(tmp_path, bridge_reads):
    # The worker's source ceiling reads v1 profiles and bridge captures only.
    root = bundle(tmp_path, [entry("2026-09-07", pinned=True), v2_entry("2026-09-10")])
    assert routes().question_coverage(root, today=date(2026, 9, 24))["cutoff_date"] == "2026-09-07"


@pytest.mark.parametrize(
    "value",
    [
        bridge_entry("2026-09-23", result_id=None),
        bridge_entry("2026-09-23", profile_id="staging_bridge_v3_20260922"),
        bridge_entry("2026-09-23", profile_id="protected_context_20260923_v1"),
        bridge_entry("2026-09-23", result_contract_version="open_intelligence_execution_result_v1"),
        bridge_entry(
            "2026-09-23",
            derivation=True,
            result_contract_version="open_intelligence_execution_result_v2",
        ),
        bridge_entry("2026-09-23", market_scope=["za", "ke"]),
        bridge_entry("2026-09-23", consumption_id="exc_short"),
        v2_entry("2026-09-10") | {"profile_id": "staging_bridge_v3_20260910", "extra": 1},
    ],
    ids=[
        "unpinned",
        "other_day",
        "v1_profile",
        "v1_version",
        "chain_version",
        "unsorted_markets",
        "bad_pin",
        "extra_field",
    ],
)
def test_a_malformed_bridge_entry_is_refused(tmp_path, bridge_reads, value):
    root = bundle(tmp_path, [entry("2026-09-07", pinned=True), value])
    with pytest.raises(ValueError):
        routes().question_coverage(root, today=date(2026, 9, 24))


def test_the_switch_is_the_one_the_worker_reads():
    source = (
        REPOSITORY / "engine/src/analysis/open_intelligence/general_question_context_admission.py"
    ).read_text()
    assert f'BRIDGE_READS_SWITCH = "{routes()._COVERAGE_BRIDGE_SWITCH}"' in source
    assert 'os.environ.get(BRIDGE_READS_SWITCH) != "enabled"' in source


@pytest.mark.parametrize("switch", [None, "enabled"])
def test_the_route_serves_the_bridge_window_the_engine_would_select(
    tmp_path, monkeypatch, switch
):
    if switch is None:
        monkeypatch.delenv("GENERAL_QUESTION_BRIDGE_READS", raising=False)
    else:
        monkeypatch.setenv("GENERAL_QUESTION_BRIDGE_READS", switch)
    pinned = entry("2026-09-07", pinned=True)
    variants = {
        "ledger": [pinned, bridge_entry("2026-09-23")],
        "chain": [pinned, bridge_entry("2026-09-23", derivation=True)],
        "v2": [pinned, v2_entry("2026-09-10")],
        "chain_pins": [
            pinned,
            bridge_entry("2026-09-23", derivation=True, result_id="exr_" + "4" * 64),
        ],
        "version": [
            pinned,
            bridge_entry(
                "2026-09-23",
                result_contract_version="open_intelligence_execution_result_v3",
            ),
        ],
        "bridge_tables": [pinned, bridge_entry("2026-09-23", snapshot_tables=["x"])],
        "chain_derivation": [
            pinned,
            bridge_entry("2026-09-23", derivation=True, derivation_id="exd_" + "3" * 63),
        ],
        "bridge_unpinned": [
            pinned,
            bridge_entry(
                "2026-09-23",
                consumption_id=None,
                manifest_sha256=None,
                result_digest=None,
                result_id=None,
            ),
        ],
    }
    cases, expected_app = [], []
    for name, entries in variants.items():
        root = bundle(tmp_path / name, [], raw=encoded(canonical(entries)))
        for today in (date(2026, 9, 23), date(2026, 9, 24)):
            cases.append([str(root.joinpath(*REGISTRY)), today.isoformat()])
            try:
                served = routes().question_coverage(root, today=today)
            except ValueError as error:
                result = ["refused", routes().coverage_refusal_code(error)]
            else:
                window = served["window"]
                result = None if window is None else [window["start"], window["end"]]
            expected_app.append((name, today.isoformat(), result))
    reasons = sorted({*REASONS, "bridge chain pins", "result contract version"})
    completed = subprocess.run(
        [sys.executable, "-c", PARITY, json.dumps(reasons)],
        input=json.dumps(cases),
        cwd=REPOSITORY / "engine",
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    engine = json.loads(completed.stdout.strip().splitlines()[-1])
    assert [
        (name, today, result)
        for (name, today, _), result in zip(expected_app, engine, strict=True)
    ] == expected_app
    window = (
        ["2026-09-10", "2026-09-23"]
        if switch == "enabled"
        else ["2026-08-25", "2026-09-07"]
    )
    assert ("ledger", "2026-09-24", window) in expected_app
    assert ("chain_derivation", "2026-09-24", ["refused", "bridge chain pins"]) in expected_app
    assert (
        "bridge_unpinned",
        "2026-09-24",
        ["refused", "authority pins must be complete or all null"],
    ) in expected_app
    assert ("chain", "2026-09-24", window) in expected_app
    assert ("v2", "2026-09-24", ["2026-08-25", "2026-09-07"]) in expected_app
    assert (
        "chain_pins",
        "2026-09-24",
        ["refused", "bridge chain pins"],
    ) in expected_app
    assert (
        "version",
        "2026-09-24",
        ["refused", "result contract version"],
    ) in expected_app
    assert (
        "bridge_tables",
        "2026-09-24",
        ["refused", "snapshot tables"],
    ) in expected_app
