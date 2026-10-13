import copy

import pytest

from core.collect import reconcile as rc
from core.collect.tests.test_reconcile import (
    START, FakeHTTP, Ledger, at, client, final, go, page, txn,
)


@pytest.fixture(autouse=True)
def synthetic_key(monkeypatch):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "synthetic-reconcile-integrity")
    monkeypatch.delenv("FORCE_RERUN", raising=False)
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)


@pytest.mark.parametrize("body", [
    {"success": True},
    {"success": True, "data": {"items": "unreadable"}},
    page([], None, has_more=True),
    page(["unreadable"], None),
    page([dict(txn("bad", at(3)), created_at="unreadable")], None),
    page([dict(txn("bad", at(3)), amount="unreadable")], None),
])
def test_unreadable_transaction_history_cannot_finish_ok(body):
    code, runs = go(Ledger(), FakeHTTP({None: body}))
    result = final(runs)
    assert code == 1
    assert result["status"] == "failed"
    assert result["counts"]["vendor_covered"] is False
    assert result["counts"]["vendor_error"]


def test_page_limit_persists_failed_run_instead_of_retry_blocking_ok(monkeypatch):
    monkeypatch.setattr(rc, "MAX_PAGES", 1)
    code, runs = go(Ledger(), FakeHTTP({None: page([txn("one", at(3))], "next")}))
    assert code == 1
    assert final(runs)["status"] == "failed"
    assert "page limit" in final(runs)["counts"]["vendor_error"]


def test_repeated_cursor_stops_without_recounting_or_waiting_for_page_limit():
    http = FakeHTTP({None: page([txn("one", at(4))], "same"),
                     "same": page([txn("two", at(3))], "same")})
    result = rc.read_transactions(client(http, Ledger()), START)
    assert result["covered"] is False
    assert "cursor" in result["error"]
    assert result["pages"] == 2
    assert [item["id"] for item in result["items"]] == ["one", "two"]


def test_overlapping_pages_count_each_transaction_once():
    shared = txn("shared", at(3), -4)
    http = FakeHTTP({None: page([shared], "next"),
                     "next": page([copy.deepcopy(shared), txn("older", at(2), -2)], None)})
    code, runs = go(Ledger(), http)
    counts = final(runs)["counts"]
    assert code == 0
    assert counts["vendor_covered"] is True
    assert counts["vendor_rows"] == 2
    assert counts["vendor_total"] == 6


def test_conflicting_transaction_identity_is_uncovered():
    http = FakeHTTP({None: page([txn("same", at(3), -4)], "next"),
                     "next": page([txn("same", at(3), -40)], None)})
    code, runs = go(Ledger(), http)
    assert code == 1
    assert final(runs)["counts"]["vendor_covered"] is False
    assert "conflicting" in final(runs)["counts"]["vendor_error"]
