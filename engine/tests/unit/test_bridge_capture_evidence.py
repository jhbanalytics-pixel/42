"""The capture evidence resolver binds route A receipts to the funded pilot before spending.

A bridge v3 capture consumes its approval only after every collection receipt its approved
receipt set names is bound to the funded Wave 1 pilot's own execution result row on the
approval ledger and to that execution read natively from Cloud Run, inside the capture
window the approved profile names. A history entry the ledger route cannot prove natively
refuses. Every chain and execution here is synthetic and unissued; no network is used.
"""

import json
import socket
from types import SimpleNamespace

import pytest
from google.auth.credentials import AnonymousCredentials
from src.analysis.open_intelligence import source_estate_bridge_evidence as evidence
from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.source_estate_bridge import (
    HISTORY_TABLES,
    MARKETS,
    bridge_policy,
)

from tests.unit.test_bridge_funded_run_binding import chains, views
from tests.unit.test_bridge_ledger_route import (
    FUNDED_EXECUTION_ID,
    FUNDED_RUN,
    funded_chain,
    funded_execution,
    receipt_item,
)
from tests.unit.test_execution_manifest_origins import load_registry

RULES = {
    "contract_version": "source_observation_semantics_v1",
    "rules": [
        {
            "source_family": "news",
            "endpoint": "articles",
            "content_type": "article",
            "time_basis": "native_event_instant",
            "event_field": "published_at",
            "missing_time_action": "withhold_event_time_claim",
            "future_time_action": "withhold_event_time_claim",
        }
    ],
}
# The capture window of cutoff 2026-09-20: from the observation window's end to the
# instant the collection clones snapshot, after the funded execution completed.
PROFILE = {
    "observation_window_end": "2026-09-21T00:00:00.000000Z",
    "collection_snapshot_as_of": "2026-09-21T00:15:31.000000Z",
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network prohibited"))


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def unavailable(lane, market, day):
    return {
        "lane": lane,
        "market": market,
        "product_date": day,
        "state": "unavailable",
        "result_ref": None,
        "product_receipt_digest": None,
        "output_digest": None,
        "row_count": None,
        "completed_at": None,
        "available_at": None,
        "reason_code": "native_completion_unrecorded",
    }


def history(day="2026-09-20"):
    entries = [unavailable(lane, market, day) for lane in HISTORY_TABLES for market in MARKETS]
    entries.sort(key=lambda row: (row["lane"], row["market"], row["product_date"]))
    return {"contract_version": "42_bridge_history_completion_set_v1", "entries": entries}


def artifacts(receipts, *, completion=None):
    return {
        "bridge_policy": canonical_bytes(bridge_policy()),
        "temporal_rules": canonical_bytes(RULES),
        "collection_receipt_set": canonical_bytes(
            {"contract_version": "42_bridge_collection_receipt_set_v1", "receipts": receipts}
        ),
        "history_completion_set": canonical_bytes(completion or history()),
    }


def resolve(values, *, read_chain, reader, catalogue, profile=PROFILE):
    return evidence.resolve_capture_bridge_evidence(
        values,
        profile=profile,
        read_chain=read_chain,
        read_funded_execution=reader,
        generation_loader=catalogue,
    )


def test_a_sound_receipt_binds_to_the_funded_result_row_and_its_execution(registry):
    chain = funded_chain(registry)
    read_chain, reader = chains(chain), views(funded_execution())
    bound = resolve(
        artifacts([receipt_item(chain)]),
        read_chain=read_chain,
        reader=reader,
        catalogue=chain["catalogue"],
    )
    assert bound == (
        {
            "execution_id": FUNDED_EXECUTION_ID,
            "pipeline_run_id": FUNDED_RUN,
            "result_json": chain["result"].canonical_result_json,
        },
    )
    assert read_chain.reads == [chain["consumption"].consumption_id]
    assert reader.reads == [FUNDED_EXECUTION_ID]


def test_a_completed_history_entry_refuses_before_any_read(registry):
    chain = funded_chain(registry)
    completion = history()
    entry = next(row for row in completion["entries"] if row["lane"] == "trend_analysis")
    entry.update(
        state="completed",
        result_ref=receipt_item(chain)["result_ref"],
        product_receipt_digest="a" * 64,
        output_digest="b" * 64,
        row_count=3,
        completed_at="2026-09-21T02:00:00.000000Z",
        available_at="2026-09-21T02:00:01.000000Z",
        reason_code=None,
    )
    read_chain, reader = chains(chain), views(funded_execution())
    with pytest.raises(ValueError, match=r"^bridge_history_evidence_unavailable$"):
        resolve(
            artifacts([receipt_item(chain)], completion=completion),
            read_chain=read_chain,
            reader=reader,
            catalogue=chain["catalogue"],
        )
    assert read_chain.reads == []
    assert reader.reads == []


def test_a_receipt_of_another_operation_is_not_a_funded_collection(registry):
    chain = funded_chain(registry)
    item = receipt_item(chain)
    item["result_ref"] = dict(item["result_ref"], operation="daily_source_collection")
    with pytest.raises(ValueError, match=r"^bridge_funded_result_not_funded$"):
        resolve(
            artifacts([item]),
            read_chain=chains(chain),
            reader=views(funded_execution()),
            catalogue=chain["catalogue"],
        )


def test_a_ledger_read_that_fails_is_unavailable(registry):
    chain = funded_chain(registry)

    def read_chain(consumption_id):
        raise PermissionError("denied")

    with pytest.raises(ValueError, match=r"^bridge_funded_result_unavailable$"):
        resolve(
            artifacts([receipt_item(chain)]),
            read_chain=read_chain,
            reader=views(funded_execution()),
            catalogue=chain["catalogue"],
        )


def test_a_denied_execution_read_keeps_its_code(registry):
    chain = funded_chain(registry)

    def reader(execution_id):
        raise ValueError("bridge_funded_execution_forbidden")

    with pytest.raises(ValueError, match=r"^bridge_funded_execution_forbidden$"):
        resolve(
            artifacts([receipt_item(chain)]),
            read_chain=chains(chain),
            reader=reader,
            catalogue=chain["catalogue"],
        )


def test_an_execution_completed_after_the_collection_snapshot_is_outside_the_window(registry):
    chain = funded_chain(registry)
    profile = dict(PROFILE, collection_snapshot_as_of="2026-09-21T00:15:30.000000Z")
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_outside_window$"):
        resolve(
            artifacts([receipt_item(chain)]),
            read_chain=chains(chain),
            reader=views(funded_execution()),
            catalogue=chain["catalogue"],
            profile=profile,
        )


def test_an_execution_started_before_the_observation_window_closed_is_outside_it(registry):
    chain = funded_chain(registry)
    profile = dict(PROFILE, observation_window_end="2026-09-21T00:00:46.000000Z")
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_outside_window$"):
        resolve(
            artifacts([receipt_item(chain)]),
            read_chain=chains(chain),
            reader=views(funded_execution()),
            catalogue=chain["catalogue"],
            profile=profile,
        )


def test_an_execution_that_started_a_day_after_the_observation_window_closed_is_outside_it(
    registry,
):
    # A profile for cutoff 2026-09-17 closes its window at 2026-09-18T00:00Z. The funded
    # execution started on 2026-09-21: it collected a later day, whatever the snapshot says.
    chain = funded_chain(registry)
    profile = dict(PROFILE, observation_window_end="2026-09-18T00:00:00.000000Z")
    read_chain, reader = chains(chain), views(funded_execution())
    with pytest.raises(ValueError, match=r"^bridge_funded_execution_outside_window$"):
        resolve(
            artifacts([receipt_item(chain)]),
            read_chain=read_chain,
            reader=reader,
            catalogue=chain["catalogue"],
            profile=profile,
        )


def test_noncanonical_artifact_bytes_refuse(registry):
    chain = funded_chain(registry)
    values = artifacts([receipt_item(chain)])
    values["collection_receipt_set"] = json.dumps(
        json.loads(values["collection_receipt_set"]), indent=1
    ).encode()
    with pytest.raises(ValueError, match=r"^bridge_evidence_invalid$"):
        resolve(
            values,
            read_chain=chains(chain),
            reader=views(funded_execution()),
            catalogue=chain["catalogue"],
        )


# The production readers the capture runs with: the runtime identity's own credentials.


def test_the_capture_evidence_readers_are_built_from_the_runtime_credentials(monkeypatch):
    from scripts.staging import capture_protected_production_snapshot as capture
    from src.analysis.open_intelligence import bridge_native_clients as native

    credentials = AnonymousCredentials()
    client = SimpleNamespace(_credentials=credentials)
    built = []
    monkeypatch.setattr(
        native,
        "ledger_reader",
        lambda warehouse: built.append(("ledger", warehouse._credentials)) or "chain",
    )

    class Executions:
        def __init__(self, value):
            built.append(("executions", value))

        def read(self, execution_id):
            return execution_id

    monkeypatch.setattr(native, "BridgeFundedExecutions", Executions)
    readers = capture._capture_evidence_readers(client)
    assert readers.read_chain == "chain"
    assert readers.read_funded_execution("x") == "x"
    assert readers.generation_loader is None
    assert built == [("ledger", credentials), ("executions", credentials)]


def test_main_wires_the_production_capture_evidence_readers(monkeypatch):
    from scripts.staging import capture_protected_production_snapshot as capture
    from src.analysis.open_intelligence import execution_approval

    seen = {}

    def main_impl(values, **seams):
        seen.update(seams["bridge_runner"].keywords)
        return 0

    monkeypatch.setattr(capture, "_main_impl", main_impl)
    monkeypatch.setattr(execution_approval, "_source_control_query", lambda: None)
    switch = "-" * 2
    argv = [switch + "cutoff-date", "2026-09-22", switch + "mode", "initial"]
    assert capture.main(argv) == 0
    assert seen["bridge_evidence_readers"] is capture._capture_evidence_readers
