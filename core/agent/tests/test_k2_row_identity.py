import copy
from datetime import date
from decimal import Decimal

import pytest

from core.agent import checks
from core.agent.tests.test_checks import FakeWarehouse, make_ctx, number


@pytest.mark.parametrize(
    "recorded,rerun",
    [
        ([{"weekday": 1, "posts": 412}], [{"weekday": 2, "posts": 412}]),
        ([{"hour": 8, "posts": 412}], [{"hour": 9, "posts": 412}]),
        ([{"cohort_id": 100, "posts": 412}], [{"cohort_id": 101, "posts": 412}]),
        ([{"cohort_id": 100.0, "posts": 412}], [{"cohort_id": 101.0, "posts": 412}]),
        ([{"scope": {"hour": 8}, "posts": 412}], [{"scope": {"hour": 9}, "posts": 412}]),
        ([{"metric": {"hour": 8, "posts": 412}}], [{"metric": {"hour": 9, "posts": 412}}]),
        ([{"metric": [{"hour": 8, "posts": 412}]}], [{"metric": [{"hour": 9, "posts": 412}]}]),
        (
            [{"hour": 8, "posts": 412}, {"hour": 9, "posts": 412}],
            [{"hour": 10, "posts": 412}, {"hour": 11, "posts": 412}],
        ),
        (
            [{"hour": 8, "posts": 412}, {"hour": 9, "posts": 412}],
            [{"hour": 8, "posts": 413}, {"hour": 9, "posts": 412}],
        ),
        ([{"cohort_id": 412, "posts": 412}], [{"cohort_id": 413, "posts": 412}]),
        ([{"hour": 8, "posts": 412, "views": 412}], [{"hour": 8, "posts": 413, "views": 412}]),
        ([{"hour": 8, "posts": 412, "ratio": 2.8}], [{"hour": 8, "posts": 412, "ratio": 2.75}]),
        ([{"day": date(2026, 9, 27), "posts": 412}], [{"day": "2026-09-27", "posts": 412}]),
        ([{"scope": {"id": Decimal("100")}, "posts": 412}], [{"scope": {"id": "100"}, "posts": 412}]),
        (
            [{"hour": 8, "posts": 412}, {"hour": 8, "posts": 412}],
            [{"hour": 8, "posts": 412}],
        ),
        (
            [{"metric": [{"hour": 8, "posts": 412}]}, {"metric": [{"hour": 8, "posts": 412}]}],
            [{"metric": [{"hour": 8, "posts": 412}]}],
        ),
    ],
)
def test_k2_holds_a_number_that_moves_to_another_numeric_cohort(recorded, rerun):
    ctx = make_ctx(rows=copy.deepcopy(recorded))
    problem = checks._number_problem(number(ctx, 412, "posts"), ctx, FakeWarehouse(rerun), {})
    assert problem is not None and "same row and column" in problem


@pytest.mark.parametrize(
    "recorded,rerun,value",
    [
        (
            [{"weekday": 1, "posts": 412}, {"weekday": 2, "posts": 131}],
            [{"posts": 131, "weekday": 2}, {"posts": 412, "weekday": 1}],
            412,
        ),
        (
            [{"scope": {"market": "ZA", "hour": 8}, "posts": 412}],
            [{"posts": 412, "scope": {"hour": 8, "market": "ZA"}}],
            412,
        ),
        (
            [{"metric": {"hour": 8, "posts": 412}}],
            [{"metric": {"posts": 412, "hour": 8}}],
            412,
        ),
        (
            [{"hour": 8, "posts": 412}, {"hour": 9, "posts": 412}],
            [{"posts": 412, "hour": 9}, {"posts": 412, "hour": 8}],
            412,
        ),
        ([{"weekday": 1, "ratio": 2.8}], [{"ratio": 2.75, "weekday": 1}], 2.8),
        (
            [{"hour": 8, "posts": 412}, {"hour": 8, "posts": 412}, {"hour": 9, "posts": 131}],
            [{"posts": 412, "hour": 8}, {"posts": 131, "hour": 9}, {"posts": 412, "hour": 8}],
            412,
        ),
        (
            [{"metric": [{"hour": 8, "posts": 412}]}, {"metric": [{"hour": 8, "posts": 412}]}],
            [{"metric": [{"posts": 412, "hour": 8}]}, {"metric": [{"posts": 412, "hour": 8}]}],
            412,
        ),
    ],
)
def test_k2_keeps_the_number_in_its_original_cohort(recorded, rerun, value):
    ctx = make_ctx(rows=copy.deepcopy(recorded))
    assert checks._number_problem(number(ctx, value, "measure"), ctx, FakeWarehouse(rerun), {}) is None
