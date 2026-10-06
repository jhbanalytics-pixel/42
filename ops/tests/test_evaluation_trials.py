import math

import pytest

from ops.evaluation import trials
from ops.evaluation.trials import paired_trial_result, validated_trial_result

PEOPLE = ("strategist_a", "strategist_b", "strategist_c")


def trial(
    participant,
    flow,
    *,
    complete=True,
    elapsed=100.0,
    unsupported=0,
    receipt_opens=1,
    clarifications=0,
):
    return {
        "participant": participant,
        "flow": flow,
        "complete": complete,
        "elapsed_seconds": elapsed,
        "unsupported_selections": unsupported,
        "receipt_opens": receipt_opens,
        "clarification_count": clarifications,
    }


def paired(
    old_elapsed=(120.0, 150.0, 130.0),
    new_elapsed=(90.0, 100.0, 95.0),
    old_unsupported=(0, 0, 0),
    new_unsupported=(0, 0, 0),
    people=PEOPLE,
):
    rows = []
    for index, person in enumerate(people):
        rows.append(
            trial(
                person,
                "old",
                elapsed=old_elapsed[index],
                unsupported=old_unsupported[index],
            )
        )
        rows.append(
            trial(
                person,
                "replacement",
                elapsed=new_elapsed[index],
                unsupported=new_unsupported[index],
            )
        )
    return rows


def test_missing_trials_do_not_pass():
    assert paired_trial_result([]) == {"trial_complete": False, "improved": False}


def test_two_participants_do_not_complete_the_trial():
    rows = paired(
        old_elapsed=(120.0, 150.0),
        new_elapsed=(90.0, 100.0),
        old_unsupported=(0, 0),
        new_unsupported=(0, 0),
        people=PEOPLE[:2],
    )
    assert paired_trial_result(rows) == {"trial_complete": False, "improved": False}


def test_four_participants_are_not_the_approved_trial():
    rows = paired(
        old_elapsed=(120.0, 150.0, 130.0, 140.0),
        new_elapsed=(90.0, 100.0, 95.0, 80.0),
        old_unsupported=(0, 0, 0, 0),
        new_unsupported=(0, 0, 0, 0),
        people=(*PEOPLE, "strategist_d"),
    )
    assert paired_trial_result(rows) == {"trial_complete": False, "improved": False}


def test_missing_replacement_row_keeps_the_trial_incomplete():
    rows = [
        row
        for row in paired()
        if row["participant"] != PEOPLE[2] or row["flow"] == "old"
    ]
    assert paired_trial_result(rows) == {"trial_complete": False, "improved": False}


def test_duplicate_trial_is_refused():
    rows = [*paired(), trial(PEOPLE[0], "old", elapsed=50.0)]
    with pytest.raises(ValueError, match="duplicate_trial"):
        paired_trial_result(rows)


def test_faster_replacement_without_more_unsupported_selections_improves():
    assert paired_trial_result(paired()) == {"trial_complete": True, "improved": True}


def test_failed_improvement_is_a_completed_failed_trial():
    rows = paired(old_elapsed=(90.0, 100.0, 95.0), new_elapsed=(120.0, 150.0, 130.0))
    assert paired_trial_result(rows) == {"trial_complete": True, "improved": False}


def test_equal_median_time_is_not_an_improvement():
    rows = paired(old_elapsed=(100.0, 100.0, 100.0), new_elapsed=(100.0, 100.0, 100.0))
    assert paired_trial_result(rows) == {"trial_complete": True, "improved": False}


def test_interrupted_task_keeps_the_trial_incomplete():
    rows = paired()
    rows[-1] = trial(PEOPLE[2], "replacement", complete=False, elapsed=10.0)
    assert paired_trial_result(rows) == {"trial_complete": False, "improved": False}


def test_unsupported_selection_increase_blocks_improvement():
    rows = paired(new_unsupported=(1, 0, 0))
    assert paired_trial_result(rows) == {"trial_complete": True, "improved": False}


def test_fewer_unsupported_selections_with_faster_median_improves():
    rows = paired(old_unsupported=(2, 1, 1), new_unsupported=(1, 0, 0))
    assert paired_trial_result(rows) == {"trial_complete": True, "improved": True}


def test_wrapper_matches_the_kernel_on_valid_rows():
    assert validated_trial_result(paired()) == paired_trial_result(paired())
    assert validated_trial_result([]) == paired_trial_result([])


@pytest.mark.parametrize("flow", ["new", "OLD", "", None, ["old"]])
def test_wrapper_rejects_unknown_flows(flow):
    rows = paired()
    rows[1]["flow"] = flow
    with pytest.raises(ValueError, match="flow_invalid"):
        validated_trial_result(rows)


@pytest.mark.parametrize("elapsed", [-1, -0.5, math.nan, math.inf, "90", None, True])
def test_wrapper_rejects_non_finite_or_negative_elapsed_time(elapsed):
    rows = paired()
    rows[0]["elapsed_seconds"] = elapsed
    with pytest.raises(ValueError, match="elapsed_seconds_invalid"):
        validated_trial_result(rows)


@pytest.mark.parametrize("field", trials.COUNT_FIELDS)
@pytest.mark.parametrize("value", [1.0, -1, "2", True, None])
def test_wrapper_rejects_non_integer_counts(field, value):
    rows = paired()
    rows[2][field] = value
    with pytest.raises(ValueError, match=f"{field}_invalid"):
        validated_trial_result(rows)


@pytest.mark.parametrize("field", trials.REQUIRED_FIELDS)
def test_wrapper_requires_every_field(field):
    rows = paired()
    del rows[3][field]
    with pytest.raises(ValueError, match="trial_field_missing"):
        validated_trial_result(rows)


def test_wrapper_requires_boolean_completion_and_named_participants():
    rows = paired()
    rows[4]["complete"] = "yes"
    with pytest.raises(ValueError, match="complete_bool_required"):
        validated_trial_result(rows)
    rows = paired()
    rows[5]["participant"] = ""
    with pytest.raises(ValueError, match="participant_invalid"):
        validated_trial_result(rows)


def test_wrapper_surfaces_duplicate_trials():
    rows = [*paired(), trial(PEOPLE[1], "replacement")]
    with pytest.raises(ValueError, match="duplicate_trial"):
        validated_trial_result(rows)
