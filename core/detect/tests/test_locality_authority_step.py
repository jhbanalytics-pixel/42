"""Where the detect locality step runs once locality_v2 can be authoritative (C4 v3 section 8.1), and the version marker
of the rule that wrote item_state.eligible (section 7.2).

Shadow: the step runs after state, because nothing in detect reads its result. Authoritative: state reads the checked
view for its own run, so the step must run between coaction and state, and the cross-tabulation against detect's
geo_status, which needs the state rows, is taken after state. The constant is patched in each test; the flip itself is
one line in core/trust/locality.py."""

import pytest

from core.detect import job, locality
from core.detect.tests import duck
from core.detect.tests.test_detect_job import FakeChain, JobClient, con, world  # noqa: F401 (con is a fixture)
from core.detect.tests.fixtures import D


def order(monkeypatch, authority):
    monkeypatch.setattr(job, "LOCALITY_AUTHORITY", authority)
    seen = []
    state, step, compare = job.run_state, job.run_locality_step, locality.shadow_comparison

    def logged(name, fn):
        def wrapper(*a, **k):
            seen.append(name)
            return fn(*a, **k)
        return wrapper

    monkeypatch.setattr(job, "run_state", logged("state", state))
    monkeypatch.setattr(job, "run_locality_step", logged("locality", step))
    monkeypatch.setattr(locality, "shadow_comparison", logged("compare", compare))
    return seen


def test_in_shadow_the_step_runs_after_state(con, monkeypatch):  # noqa: F811
    world(con)
    seen = order(monkeypatch, "v1")
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert seen == ["state", "locality", "compare"]


def test_when_authoritative_the_step_runs_before_state_and_the_comparison_after_it(con, monkeypatch):  # noqa: F811
    world(con)
    seen = order(monkeypatch, "v2")
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert seen == ["locality", "state", "compare"]
    assert counts["locality"]["keys_refused"] == 0 and counts["locality_shadow"]["by_status"]


def test_state_reads_the_rows_the_step_wrote_in_the_same_run(con, monkeypatch):  # noqa: F811
    world(con)
    order(monkeypatch, "v2")
    job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    rows = duck.query(con, "SELECT s.item_id, s.eligible, s.locality_basis, s.locality_status "
                           "FROM {core}.item_state s WHERE s.metric_date = @d", {"d": D})
    assert rows and {(r["locality_basis"], r["locality_status"]) for r in rows} == {("locality_v2.1", "market_unconfirmed")}


def test_a_failed_step_leaves_every_row_missing_and_the_run_going(con, monkeypatch):  # noqa: F811
    world(con)
    order(monkeypatch, "v2")
    monkeypatch.setattr(locality, "run_locality_step", lambda *a, **k: 1 / 0)
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    assert counts["locality"]["status"] == "failed"
    rows = duck.query(con, "SELECT s.eligible, s.locality_status FROM {core}.item_state s WHERE s.metric_date = @d", {"d": D})
    assert rows and {(r["eligible"], r["locality_status"]) for r in rows} == {(True, "missing")}


@pytest.mark.parametrize(("authority", "version"), [("v1", "warmup-1"), ("v2", "warmup-2")])
def test_the_rule_version_names_the_rule_that_writes_eligible(authority, version):
    assert job.rule_version_for(authority) == version


def test_the_job_constant_is_the_version_of_the_authority_constant():
    assert job.RULE_VERSION == job.rule_version_for(job.LOCALITY_AUTHORITY)


def test_the_authority_constant_is_one_of_the_two_rules_and_the_job_reads_that_constant():
    from core.trust import locality as trust

    assert trust.LOCALITY_AUTHORITY in ("v1", "v2")
    assert job.LOCALITY_AUTHORITY == trust.LOCALITY_AUTHORITY


@pytest.mark.parametrize("value", ["v1", "v2"])
def test_the_suite_fixture_moves_the_authority_everywhere_it_was_read(monkeypatch, value):
    from core.brief import job as brief_job
    from core.conftest import set_locality_authority
    from core.trust import locality as trust

    set_locality_authority(monkeypatch, value)
    assert trust.LOCALITY_AUTHORITY == job.LOCALITY_AUTHORITY == value
    assert job.RULE_VERSION == brief_job.RULE_VERSION == job.rule_version_for(value)
    assert job.run.__kwdefaults__["rule_version"] == job.rule_version_for(value)


def remove_locality_views(con, monkeypatch):
    """The views step failed and the views are not there: a release that landed the image before the views."""
    con.execute("DROP VIEW core.v_item_locality_current")
    con.execute("DROP VIEW core.v_item_locality_checked")

    def refused(*a, **k):
        raise RuntimeError("Not found: Table item_locality_verified")

    monkeypatch.setattr(job.sqlrun, "apply_locality_views", refused)


def test_in_shadow_detect_runs_when_the_locality_views_are_absent(con, monkeypatch):  # noqa: F811
    """C4 v3 section 10: a failed view never changes an outcome in shadow. State no longer reads the checked view there."""
    world(con)
    baseline = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    expected = duck.query(con, "SELECT item_id, eligible, locality_basis FROM {core}.item_state ORDER BY item_id", {})
    con.execute("DELETE FROM core.item_state")
    order(monkeypatch, "v1")
    remove_locality_views(con, monkeypatch)
    counts = job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
    got = duck.query(con, "SELECT item_id, eligible, locality_basis FROM {core}.item_state ORDER BY item_id", {})
    assert counts["locality_views"]["status"] == "failed" and counts["locality_shadow"]["status"] == "failed"
    assert counts["item_state"] == baseline["item_state"] and got == expected and got
    assert {r["locality_basis"] for r in got} == {"v1"}


def test_when_authoritative_a_missing_locality_view_still_fails_state(con, monkeypatch):  # noqa: F811
    world(con)
    order(monkeypatch, "v2")
    remove_locality_views(con, monkeypatch)
    with pytest.raises(Exception):
        job.run(JobClient(con), D, chain=FakeChain(con), core="core", agent="agent")
