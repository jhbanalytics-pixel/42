import asyncio
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api import question_worker_protocol as protocol
from src.api import question_worker_store as store
from src.api import question_worker_process as process
from src.api.question_worker_result import ResultVerificationError
from tests.unit.test_fieldwork_v2 import SCOPE, observation, row
from tests.unit.test_question_worker_deadline import raw_request
from tests.unit.test_question_worker_result import EXPECTED, canonical, raw_fixture


def observe_input(detail=False):
    scope = copy.deepcopy(SCOPE)
    if detail:
        scope["market_scope"] = ["ke", "ng", "za"]
    return dict(
        contract_version="general_question_observe_request_v1",
        scope=scope,
        request_id=EXPECTED["request_id"] if detail else None,
    )


def inventory():
    bundle = observation([row()])
    return dict(
        contract_version="general_question_observe_reply_v1",
        mode="inventory",
        ledger_generation="3",
        coverage=bundle["coverage"],
        rows=bundle["rows"],
    )


def detail(terminal=False):
    request = json.loads(raw_request())
    intake = dict(
        contract_version="general_question_intake_context_v1",
        request_id=request["request_id"],
        request_digest=request["request_digest"],
        selected_market="za",
        captured_at=request["as_of"],
    )
    intake["intake_digest"] = hashlib.sha256(canonical(intake)).hexdigest()
    invocation = EXPECTED | {"intake_digest": intake["intake_digest"]}
    reply = dict(
        contract_version="general_question_observe_reply_v1",
        mode="detail",
        ledger_generation="3",
        invocation=invocation,
        request_generation="11",
        intake_generation="12",
        observed_state="unconfirmed",
        result_pointer=None,
        reserved_microusd=100000,
        missing_work=["execution_unconfirmed"],
    )
    records = {
        ("request.json", 11): raw_request(),
        ("intake.json", 12): canonical(intake),
    }
    if terminal:
        state = "held" if terminal == "held" else "unavailable"
        result = json.loads(raw_fixture(state))
        result["intake_digest"] = intake["intake_digest"]
        result["response"]["intelligence"]["resolved_scope"]["market_scope"] = [
            "za"
        ]
        raw = canonical(result)
        digest = hashlib.sha256(raw).hexdigest()
        reply.update(
            observed_state=state,
            missing_work=[],
            result_pointer={
                **{
                    key: invocation[key]
                    for key in ("request_id", "request_digest", "intake_digest")
                },
                "result_digest": digest,
                "result_generation": "17",
                "state": state,
            },
        )
        records[(f"results/{digest}.json", 17)] = raw
    return reply, records


def rebound_terminal_detail(*, selected_market, request_changes=None, result_changes=None):
    reply, records = detail(True)
    request = json.loads(records[("request.json", 11)])
    request.update(request_changes or {})
    request.pop("request_digest")
    request["request_digest"] = hashlib.sha256(canonical(request)).hexdigest()
    records[("request.json", 11)] = canonical(request)

    intake = json.loads(records[("intake.json", 12)])
    intake.update(selected_market=selected_market, request_digest=request["request_digest"])
    intake.pop("intake_digest")
    intake["intake_digest"] = hashlib.sha256(canonical(intake)).hexdigest()
    records[("intake.json", 12)] = canonical(intake)

    old_key = next(key for key in records if key[0].startswith("results/"))
    result = json.loads(records.pop(old_key))
    result.update(
        request_digest=request["request_digest"], intake_digest=intake["intake_digest"]
    )
    result["response"]["intelligence"]["request_digest"] = request["request_digest"]
    result["response"]["intelligence"]["resolved_scope"].update(
        result_changes or {}
    )
    raw = canonical(result)
    digest = hashlib.sha256(raw).hexdigest()
    records[(f"results/{digest}.json", 17)] = raw
    reply["invocation"].update(
        request_digest=request["request_digest"], intake_digest=intake["intake_digest"]
    )
    reply["result_pointer"].update(
        request_digest=request["request_digest"],
        intake_digest=intake["intake_digest"],
        result_digest=digest,
    )
    return reply, records


def test_observe_encoder_preserves_inventory_and_detail_without_execution_fields():
    for value in (observe_input(), observe_input(True)):
        assert json.loads(protocol.encode_worker_input("observe", value)) == value


def test_observe_decoder_keeps_scoped_counts_and_compact_pinned_detail():
    value = inventory()
    assert protocol.decode_observe_reply(canonical(value), observe_input()) == value
    value, _ = detail(True)
    assert protocol.decode_observe_reply(canonical(value), observe_input(True)) == value


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(ledger_generation=None),
        lambda value: value["coverage"].update(known_count=True),
        lambda value: value["rows"][0].update(status="running"),
        lambda value: value["rows"][0].update(market_scope=["ng"]),
        lambda value: value["rows"][0]["next_operation"].update(entity_id="inv_other"),
        lambda value: value.update(unexpected="private"),
    ],
)
def test_invalid_inventory_cannot_become_operations(change):
    value = inventory()
    change(value)
    with pytest.raises(protocol.WorkerProtocolError):
        protocol.decode_observe_reply(canonical(value), observe_input())


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(request_generation="0"),
        lambda value: value.update(reserved_microusd=True),
        lambda value: value.update(observed_state="running"),
        lambda value: value.update(missing_work=["private raw exception"]),
        lambda value: value.update(
            result_pointer={"digest": "f" * 64, "generation": "17"}
        ),
        lambda value: value["invocation"].update(
            request_id="00000000-0000-0000-0000-000000000002"
        ),
    ],
)
def test_invalid_detail_authority_refuses(change):
    value, _ = detail()
    change(value)
    with pytest.raises(protocol.WorkerProtocolError):
        protocol.decode_observe_reply(canonical(value), observe_input(True))


def test_observe_ack_preserves_actual_state_instead_of_execution_terminal_label():
    value, _ = detail(True)
    value["result_pointer"]["state"] = "terminal"
    with pytest.raises(protocol.WorkerProtocolError):
        protocol.decode_observe_reply(canonical(value), observe_input(True))


class PinnedStore:
    def __init__(self, records):
        self.records = records
        self.reads = []

    def bucket(self, name):
        assert name == store._BUCKET
        return self

    def blob(self, name, *, generation):
        relative = name.removeprefix(store._PREFIX + EXPECTED["request_id"] + "/")
        self.reads.append((relative, generation))
        requested = generation
        if generation is None:
            # Only the plan is read unpinned: the observer names no generation
            # for it, and it is written once and bound by its digest instead.
            assert relative == "plan.json"
            [generation] = [held for path, held in self.records if path == relative]
        raw = self.records[(relative, generation)]

        def reload(**kwargs):
            assert (
                kwargs["if_generation_match"] == requested and kwargs["retry"] is None
            )

        def download(**kwargs):
            assert (
                kwargs["if_generation_match"] == generation and kwargs["raw_download"]
            )
            return raw

        return SimpleNamespace(
            generation=generation,
            size=len(raw),
            reload=reload,
            download_as_bytes=download,
        )


DEFAULT_WINDOW = {"start": "2026-08-23", "end": "2026-09-05", "closed": True}
PLANNED_WINDOW = {"start": "2026-08-30", "end": "2026-09-05", "closed": True}
CLARIFICATION = (
    "No supported default observation window is available. "
    "Please specify the dates to investigate."
)


def planned_detail(plan_window, *, reply_window=DEFAULT_WINDOW, tamper=None):
    """A stored failure whose record binds a plan, as expiry and cancel write it.

    Both keep the stored plan's digest; a plan that could not resolve a window
    leaves the reply with the engine's default window instead.
    """
    reply, records = detail(True)
    old_key = next(key for key in records if key[0].startswith("results/"))
    result = json.loads(records.pop(old_key))
    invocation = reply["invocation"]
    plan = {
        "contract_version": "general_question_plan_v1",
        "request_id": invocation["request_id"],
        "request_digest": invocation["request_digest"],
        "intake_digest": invocation["intake_digest"],
        "status": "ready" if plan_window else "needs_clarification",
        "window": plan_window,
        "clarification": None if plan_window else CLARIFICATION,
        "markets": ["za"],
    }
    plan["plan_digest"] = hashlib.sha256(canonical(plan)).hexdigest()
    result["plan_digest"] = plan["plan_digest"]
    result["response"]["intelligence"]["window"] = reply_window
    if tamper:
        tamper(plan)
    records[("plan.json", 23)] = canonical(plan)
    raw = canonical(result)
    digest = hashlib.sha256(raw).hexdigest()
    records[(f"results/{digest}.json", 17)] = raw
    reply["result_pointer"].update(result_digest=digest)
    return reply, records


def read_detail(reply, records):
    client = PinnedStore(records)
    result = store.read_observed_question_detail(
        client,
        observe_input=observe_input(True),
        observed_reply=reply,
        deadline=time.monotonic() + 10,
    )
    return result, client


@pytest.mark.parametrize("plan_window", [None, PLANNED_WINDOW])
def test_detail_reads_the_window_the_bound_plan_names(plan_window):
    result, client = read_detail(*planned_detail(plan_window))
    assert result["plan_window"] == plan_window
    assert ("plan.json", None) in client.reads


@pytest.mark.parametrize("terminal", [False, True])
def test_detail_without_a_bound_plan_reads_no_plan(terminal):
    result, client = read_detail(*detail(terminal))
    assert result["plan_window"] is None
    assert not any(path == "plan.json" for path, _ in client.reads)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda plan: plan.update(window=PLANNED_WINDOW),
        lambda plan: plan.update(request_id="00000000-0000-4000-8000-000000000002"),
        lambda plan: plan.update(plan_digest="f" * 64),
    ],
)
def test_a_plan_that_does_not_match_its_bound_digest_is_refused(tamper):
    with pytest.raises(ResultVerificationError):
        read_detail(*planned_detail(None, tamper=tamper))


def test_a_bound_plan_that_is_missing_is_refused():
    reply, records = planned_detail(PLANNED_WINDOW)
    del records[("plan.json", 23)]
    with pytest.raises(ResultVerificationError):
        read_detail(reply, records)


@pytest.mark.parametrize("terminal", [False, True, "held"])
def test_detail_reads_expired_history_without_recovery_or_new_deadline(terminal):
    reply, records = detail(terminal)
    client = PinnedStore(records)
    result = store.read_observed_question_detail(
        client,
        observe_input=observe_input(True),
        observed_reply=reply,
        deadline=time.monotonic() + 10,
    )
    assert result["request_bytes"] == raw_request()
    assert result["intake_bytes"] == records[("intake.json", 12)]
    assert (result["result_record"] is not None) == bool(terminal)
    assert client.reads[:2] == [("request.json", 11), ("intake.json", 12)]
    assert len(client.reads) == (3 if terminal else 2)


@pytest.mark.parametrize("name", ["request.json", "intake.json"])
def test_detail_tampering_stops_before_result_read(name):
    reply, records = detail(True)
    key = (name, 11 if name == "request.json" else 12)
    value = json.loads(records[key])
    value["question" if name == "request.json" else "selected_market"] = "changed"
    records[key] = canonical(value)
    client = PinnedStore(records)
    with pytest.raises(ResultVerificationError):
        store.read_observed_question_detail(
            client,
            observe_input=observe_input(True),
            observed_reply=reply,
            deadline=time.monotonic() + 10,
        )
    assert not any(path.startswith("results/") for path, _ in client.reads)


@pytest.mark.parametrize("is_detail", [False, True])
def test_actual_observe_child_does_not_call_result_verifier(
    tmp_path, monkeypatch, is_detail
):
    entry = tmp_path / "scripts/staging/run_general_question_worker.py"
    entry.parent.mkdir(parents=True)
    value = detail(True)[0] if is_detail else inventory()
    entry.write_text(
        "import json,sys\nassert sys.argv[1:] == ['--operation','observe']\n"
        f"assert json.load(sys.stdin)['request_id'] == {observe_input(is_detail)['request_id']!r}\n"
        f"sys.stdout.write({json.dumps(value)!r})\n",
        encoding="utf-8",
    )
    native = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        kwargs.pop("start_new_session", None)
        return await native(*args, **kwargs)

    async def stop(child):
        if child.returncode is None:
            child.kill()
            await child.wait()

    async def forbidden(_reply):
        raise AssertionError("observe invoked execution result callback")

    monkeypatch.setattr(
        process, "os", SimpleNamespace(name="posix", environ=os.environ)
    )
    monkeypatch.setattr(process, "_terminate_group", stop)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    result = asyncio.run(
        process.invoke_engine(
            tmp_path,
            Path(sys.executable),
            "observe",
            observe_input(is_detail),
            deadline=time.monotonic() + 10,
            verify_runtime=lambda: None,
            verify_result=forbidden,
        )
    )
    assert result.reply == value


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(request_id="../private"),
        lambda value: value.update(contract_version="other"),
        lambda value: value["scope"].update(market_scope=[]),
        lambda value: value["scope"].update(
            audience_lens_ids=["duplicate", "duplicate"]
        ),
    ],
)
def test_bad_observation_input_refuses_before_worker_start(change):
    value = observe_input()
    change(value)
    with pytest.raises(protocol.WorkerProtocolError):
        protocol.encode_worker_input("observe", value)


def test_unavailable_inventory_has_no_invented_ledger_generation():
    from src.api import fieldwork

    value = inventory()
    value.update(
        ledger_generation=None, coverage=fieldwork._unavailable_coverage_v2(), rows=[]
    )
    assert protocol.decode_observe_reply(canonical(value), observe_input()) == value
    with pytest.raises(protocol.WorkerProtocolError):
        protocol.decode_observe_reply(
            b'{"mode":"inventory","mode":"detail"}', observe_input()
        )
    with pytest.raises(protocol.WorkerProtocolError):
        protocol.decode_observe_reply(
            b" " * (protocol.STDOUT_LIMIT + 1), observe_input()
        )


def test_detail_scope_cannot_be_widened_by_valid_bytes():
    reply, records = detail(True)
    value = observe_input(True)
    value["scope"]["brand_config_id"] = "another_brand"
    client = PinnedStore(records)
    with pytest.raises(ResultVerificationError, match="observation_scope_invalid"):
        store.read_observed_question_detail(
            client,
            observe_input=value,
            observed_reply=reply,
            deadline=time.monotonic() + 10,
        )
    assert len(client.reads) == 2


def test_selected_market_accepts_an_unplanned_result_over_the_full_request():
    # Contract correction, 24 Sept 2026: this case used to be refused. The
    # engine keeps the request's original market scope for an expired request
    # that never bound a plan (engine/tests/unit/test_general_question_result.py
    # pins that), so a selected market in the request must still read the
    # unplanned result written over the request's full market list.
    reply, records = rebound_terminal_detail(
        selected_market="za", result_changes={"market_scope": ["ke", "ng", "za"]}
    )
    result = store.read_observed_question_detail(
        PinnedStore(records),
        observe_input=observe_input(True),
        observed_reply=reply,
        deadline=time.monotonic() + 10,
    )
    assert result["plan_window"] is None
    assert result["result_record"]["plan_digest"] is None
    assert result["result_record"]["response"]["intelligence"]["resolved_scope"][
        "market_scope"
    ] == ["ke", "ng", "za"]


def test_selected_market_refuses_a_planned_result_over_the_full_request():
    reply, records = planned_detail(PLANNED_WINDOW)
    old_key = next(key for key in records if key[0].startswith("results/"))
    result = json.loads(records.pop(old_key))
    result["response"]["intelligence"]["resolved_scope"]["market_scope"] = [
        "ke",
        "ng",
        "za",
    ]
    raw = canonical(result)
    digest = hashlib.sha256(raw).hexdigest()
    records[(f"results/{digest}.json", 17)] = raw
    reply["result_pointer"].update(result_digest=digest)
    with pytest.raises(ResultVerificationError, match="observation_binding_invalid"):
        read_detail(reply, records)


@pytest.mark.parametrize(
    "request_changes, result_markets",
    [
        ({}, ["ng", "za"]),
        ({}, ["ke", "za"]),
        ({}, ["za", "ng", "ke"]),
        ({"market_scope": ["ng", "za"]}, ["ke", "ng", "za"]),
    ],
)
def test_selected_market_refuses_an_unplanned_result_off_the_full_request(
    request_changes, result_markets
):
    reply, records = rebound_terminal_detail(
        selected_market="za",
        request_changes=request_changes,
        result_changes={"market_scope": result_markets},
    )
    with pytest.raises(ResultVerificationError, match="observation_binding_invalid"):
        store.read_observed_question_detail(
            PinnedStore(records),
            observe_input=observe_input(True),
            observed_reply=reply,
            deadline=time.monotonic() + 10,
        )


def test_selected_market_outside_the_request_refuses_a_full_request_result():
    reply, records = rebound_terminal_detail(
        selected_market="ke",
        request_changes={"market_scope": ["ng", "za"]},
        result_changes={"market_scope": ["ng", "za"]},
    )
    with pytest.raises(ResultVerificationError, match="observation_binding_invalid"):
        store.read_observed_question_detail(
            PinnedStore(records),
            observe_input=observe_input(True),
            observed_reply=reply,
            deadline=time.monotonic() + 10,
        )


@pytest.mark.parametrize(
    "selected_market, markets",
    [("za", ["za"]), (None, ["ke", "ng", "za"])],
)
def test_selected_and_all_market_results_remain_bound(selected_market, markets):
    reply, records = rebound_terminal_detail(
        selected_market=selected_market, result_changes={"market_scope": markets}
    )
    result = store.read_observed_question_detail(
        PinnedStore(records),
        observe_input=observe_input(True),
        observed_reply=reply,
        deadline=time.monotonic() + 10,
    )
    assert result["result_record"]["response"]["intelligence"][
        "resolved_scope"
    ]["market_scope"] == markets


@pytest.mark.parametrize(
    "request_changes, result_changes",
    [
        ({"market_scope": ["za"]}, {"market_scope": ["ng"]}),
        ({}, {"audience_lens_ids": ["audience_other"]}),
        ({}, {"client_scope_id": "client_other"}),
        ({}, {"brand_config_id": "brand_other"}),
        ({}, {"theme_id": "theme_other"}),
    ],
)
def test_result_cannot_escape_original_request_scope_or_lenses(
    request_changes, result_changes
):
    reply, records = rebound_terminal_detail(
        selected_market=None,
        request_changes=request_changes,
        result_changes=result_changes,
    )
    with pytest.raises(ResultVerificationError, match="observation_binding_invalid"):
        store.read_observed_question_detail(
            PinnedStore(records),
            observe_input=observe_input(True),
            observed_reply=reply,
            deadline=time.monotonic() + 10,
        )


def test_expired_reader_deadline_performs_no_storage_read():
    reply, records = detail()
    client = PinnedStore(records)
    with pytest.raises(ResultVerificationError, match="result_deadline_reached"):
        store.read_observed_question_detail(
            client,
            observe_input=observe_input(True),
            observed_reply=reply,
            deadline=time.monotonic() - 1,
        )
    assert client.reads == []


@pytest.mark.parametrize(
    "codes, expected",
    [
        (["observation_request_unavailable"], "observation_request_unavailable"),
        (["observation_invalid"], "observation_invalid"),
        (["PRIVATE_RAW_TEXT"], "worker_exit_failed"),
        (
            ["observation_invalid", "observation_request_unavailable"],
            "worker_exit_failed",
        ),
    ],
)
def test_actual_observer_child_errors_are_bounded(
    tmp_path, monkeypatch, codes, expected
):
    entry = tmp_path / "scripts/staging/run_general_question_worker.py"
    entry.parent.mkdir(parents=True)
    stderr = "".join(json.dumps({"error": code}) + "\n" for code in codes)
    entry.write_text(
        f"import sys\nsys.stdin.read()\nsys.stderr.write({stderr!r})\nsys.exit(1)\n",
        encoding="utf-8",
    )
    native = asyncio.create_subprocess_exec

    async def launch(*args, **kwargs):
        kwargs.pop("start_new_session", None)
        return await native(*args, **kwargs)

    async def stop(child):
        if child.returncode is None:
            child.kill()
            await child.wait()

    monkeypatch.setattr(
        process, "os", SimpleNamespace(name="posix", environ=os.environ)
    )
    monkeypatch.setattr(process, "_terminate_group", stop)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    with pytest.raises(process.WorkerProcessError) as error:
        asyncio.run(
            process.invoke_engine(
                tmp_path,
                Path(sys.executable),
                "observe",
                observe_input(True),
                deadline=time.monotonic() + 10,
                verify_runtime=lambda: None,
            )
        )
    assert error.value.reason == expected
    assert "PRIVATE" not in str(error.value)
