"""Coverage's words while no scorecard exists say when the learn job writes one (inventory FE-3 and API-4)."""
from core.api import coverage
from core.setup import schedule


def test_no_scorecard_words_name_the_learn_job_schedule():
    (cron,) = [cron for name, cron, job, _ in schedule.SCHEDULES if job == "f42-learn"]
    assert cron == "30 7 * * 1" and schedule.TIME_ZONE == "Africa/Johannesburg"
    assert coverage.NO_SCORECARD == ("No weekly scorecard yet. The learn job writes one every Monday at 07:30 SAST, "
                                     "for the week before")


def test_no_scorecard_words_promise_no_amount_of_data():
    assert "four weeks" not in coverage.NO_SCORECARD
    assert not coverage.NO_SCORECARD.endswith(".")  # the page adds the full stop
