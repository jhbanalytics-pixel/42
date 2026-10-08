"""Retry on the same commit (W8-REL 2.2, RT-01 to RT-05): a second attempt takes the next image tag, declares the first attempt's
revisions, and is refused while the first attempt's tags are still on a service. These nodes collect in the deploy partition (R1)."""
import pytest

from core.setup.release import services_only as so
from core.setup.tests.release_world import CAND_REV, CANON, HEALTHY, RID, SHORT12, Scenario
from core.setup.tests.test_bound_readback_services import RID2, SERVICES, second_attempt, stops_with


def test_rt01_a_second_attempt_on_the_commit_builds_its_own_image_tag(tmp_path):
    first = Scenario(tmp_path / "first")
    first.to("Freeze")  # the first attempt built and registered f42-web:<sha12>-01
    second = Scenario(tmp_path / "second", world=first.world, release_id=RID2, baseline_from=first)
    assert second.tag().endswith(f"{SHORT12}-02") and first.tag().endswith(f"{SHORT12}-01")
    assert second.run("BeforeAnyWrite")["phase"] == "BeforeAnyWrite"


def test_rt02_a_failed_attempt_with_its_tags_removed_and_declared_lets_the_next_pass_before_candidate(tmp_path):
    first = Scenario(tmp_path / "first")
    first.to("AfterSmoke")
    for name in SERVICES:
        first.world.remove_tag(name)
    second = second_attempt(tmp_path, first)
    second.to("BeforeCandidate")


def test_rt03_a_failed_attempt_after_the_pin_passes_and_the_pin_is_idempotent(tmp_path):
    first = Scenario(tmp_path / "first")
    first.to("BeforeCandidate")
    second = Scenario(tmp_path / "second", world=first.world, release_id=RID2, baseline_from=first)
    second.to("BeforeCandidate")
    second.world.pin()
    assert second.run("BeforeCandidate")["phase"] == "BeforeCandidate"


def test_rt04_the_first_attempts_revisions_without_priorAttempts_are_unrelated(tmp_path):
    first = Scenario(tmp_path / "first")
    first.to("AfterSmoke")
    for name in SERVICES:
        first.world.remove_tag(name)
    second = second_attempt(tmp_path, first, declare_prior=False)
    stops_with(second, "BeforeAnyWrite", "UNRELATED_REVISION")


def test_rt05_the_first_attempts_tags_still_present_stop_the_second(tmp_path):
    first = Scenario(tmp_path / "first")
    first.to("AfterSmoke")
    second = second_attempt(tmp_path, first)
    stops_with(second, "BeforeAnyWrite", "TAG_MAPPING")
