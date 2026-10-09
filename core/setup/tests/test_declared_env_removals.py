"""Release A declares that two of f42-agent's environment variables are expected to be removed by its deploy. They are on
every live revision and deploy_flags.env does not set them, so the preservation readback treats their removal as declared,
not as a failure, and treats anything else that changes as a stop. The names are kept out of the repository: the engine
holds the SHA-256 of each, and the tests that need the names read them from a file outside it (release_world.declared_names).
The readback records names and the words removed or still_present, never a value.
"""
import hashlib
import json
import re

import pytest

from core.setup.release import services_only as so
from core.setup.tests.release_world import AGENT_ENV, CAND_REV, VERTEX_NAMES, Scenario, World

SENTINEL = "SENTINEL-DECLARED-VALUE-9d41"
needs_names = pytest.mark.skipif(
    len(VERTEX_NAMES) != 2,
    reason="the declared names file (wave8/release-packet/declared-env-removals.txt, or F42_DECLARED_ENV_NAMES_FILE) is absent; "
           "the two names are kept out of the repository")


def test_the_declaration_is_two_digests_for_the_agent_and_nothing_for_any_other_service():
    assert set(so.DECLARED_ENV_REMOVAL_DIGESTS) == {"f42-agent"}
    digests = so.DECLARED_ENV_REMOVAL_DIGESTS["f42-agent"]
    assert len(digests) == 2 and len(set(digests)) == 2
    assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in digests)


def test_a_name_is_declared_when_its_digest_is_listed_and_not_otherwise():
    assert so.declared_removal("f42-agent", "F42_DATA") is False
    assert so.declared_removal("f42-api", "F42_DATA") is False
    assert so.declared_removal("f42-agent", "") is False


@needs_names
def test_the_two_digests_are_the_sha256_of_the_two_names_in_lower_case_hex_and_the_names_are_not_in_the_engine():
    digests = {hashlib.sha256(name.encode("utf-8")).hexdigest() for name in VERTEX_NAMES}
    assert digests == set(so.DECLARED_ENV_REMOVAL_DIGESTS["f42-agent"])
    for name in VERTEX_NAMES:
        assert so.declared_removal("f42-agent", name) is True and so.declared_removal("f42-api", name) is False
        assert name.lower() not in open(so.__file__, encoding="utf-8").read().lower()


def agent_candidate(tmp_path, *, remove=(), override=None):
    s = Scenario(tmp_path)
    s.to("BeforeCandidate")
    s.world.deploy_candidate("f42-agent", remove_env=remove, env_override=override)
    s.world.deploy_candidate("f42-api")
    return s


@needs_names
def test_the_candidate_that_lost_both_declared_variables_passes_and_the_readback_says_removed(tmp_path):
    s = agent_candidate(tmp_path, remove=VERTEX_NAMES)
    result = s.run("BeforeSmoke")
    assert result["observations"]["declared_env_removals"]["f42-agent"] == {name: "removed" for name in VERTEX_NAMES}


@needs_names
def test_the_candidate_that_kept_both_unchanged_also_passes_and_the_readback_says_still_present(tmp_path):
    s = agent_candidate(tmp_path)
    result = s.run("BeforeSmoke")
    assert result["observations"]["declared_env_removals"]["f42-agent"] == {name: "still_present" for name in VERTEX_NAMES}


@needs_names
@pytest.mark.parametrize("index", [0, 1])
def test_a_candidate_that_lost_only_one_of_them_passes(tmp_path, index):
    s = agent_candidate(tmp_path, remove=(VERTEX_NAMES[index],))
    removals = s.run("BeforeSmoke")["observations"]["declared_env_removals"]["f42-agent"]
    assert removals == {name: ("removed" if i == index else "still_present") for i, name in enumerate(VERTEX_NAMES)}


@needs_names
@pytest.mark.parametrize("index", [0, 1])
def test_a_declared_variable_that_is_still_there_with_another_value_is_a_stop(tmp_path, index):
    s = agent_candidate(tmp_path, override={VERTEX_NAMES[index]: "another-value"})
    assert s.stop("BeforeSmoke").code == "ENV"


@needs_names
@pytest.mark.parametrize("name", ["F42_DATA", "GEMINI_MODEL", "F42_T2_READY", "MODEL_PROVIDER"])
def test_removing_any_other_variable_is_still_a_stop_even_alongside_the_declared_removals(tmp_path, name):
    s = agent_candidate(tmp_path, remove=(*VERTEX_NAMES, name))
    assert s.stop("BeforeSmoke").code == "ENV"


@needs_names
def test_an_added_variable_next_to_the_declared_removals_is_a_stop(tmp_path):
    s = agent_candidate(tmp_path, remove=VERTEX_NAMES, override={"EXTRA_SETTING": "x"})
    assert s.stop("BeforeSmoke").code == "ENV"


def _world_with_the_names_on_the_api():
    world = World()
    for name in VERTEX_NAMES:
        world.set_env("f42-api-00041-lns", name, "1")
        world.svc["f42-api"]["env"][name] = "1"
    return world


@needs_names
def test_the_declaration_does_not_cover_the_api_service(tmp_path):
    s = Scenario(tmp_path, world=_world_with_the_names_on_the_api())
    s.to("BeforeCandidate")
    s.world.deploy_candidate("f42-agent")
    s.world.deploy_candidate("f42-api", remove_env=VERTEX_NAMES)
    assert s.stop("BeforeSmoke").code == "ENV"


@needs_names
def test_the_declared_removal_holds_through_every_candidate_phase(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforeCandidate")
    s.world.deploy_candidate("f42-agent", remove_env=VERTEX_NAMES)
    s.world.deploy_candidate("f42-api")
    for phase in ("BeforeSmoke", "AfterSmoke", "BeforePromotion", "AfterAgentPromotion", "AfterPromotion"):
        if phase != "BeforeSmoke":
            s.step(phase)
        assert s.run(phase)["phase"] == phase


@needs_names
def test_the_template_may_carry_either_form_too(tmp_path):
    s = agent_candidate(tmp_path, remove=VERTEX_NAMES)
    s.world.svc["f42-agent"]["env"].update({name: AGENT_ENV[name] for name in VERTEX_NAMES})  # template still has them
    assert s.run("BeforeSmoke")["phase"] == "BeforeSmoke"


@needs_names
def test_the_manifest_records_the_names_and_an_expected_hash_that_does_not_depend_on_the_outcome(tmp_path):
    s = Scenario(tmp_path)
    s.to("Freeze")
    candidate = s.manifest()["candidates"]["f42-agent"]
    assert candidate["env_removals_declared"] == sorted(VERTEX_NAMES)
    assert s.manifest()["candidates"]["f42-api"]["env_removals_declared"] == []
    again = Scenario(tmp_path / "again")
    again.to("Freeze")
    assert again.manifest()["candidates"]["f42-agent"]["expected_env_sha256"] == candidate["expected_env_sha256"]


@needs_names
def test_a_baseline_without_the_declared_variables_has_nothing_to_remove(tmp_path):
    world = World()
    for name in VERTEX_NAMES:
        world.drop_env("f42-agent-00047-677", name)
        world.svc["f42-agent"]["env"].pop(name)
    s = Scenario(tmp_path, world=world)
    s.to("BeforeCandidate")
    s.world.deploy_candidate("f42-agent")
    s.world.deploy_candidate("f42-api")
    assert s.manifest()["candidates"]["f42-agent"]["env_removals_declared"] == []
    assert "declared_env_removals" not in s.run("BeforeSmoke")["observations"]


@needs_names
def test_no_value_of_a_declared_variable_reaches_an_output_or_a_file(tmp_path, capsys):
    world = World()
    for name in VERTEX_NAMES:
        world.set_env("f42-agent-00047-677", name, SENTINEL)
        world.svc["f42-agent"]["env"][name] = SENTINEL
    s = Scenario(tmp_path, world=world)
    s.to("BeforeCandidate")
    world.deploy_candidate("f42-agent", remove_env=(VERTEX_NAMES[1],))
    world.deploy_candidate("f42-api")
    result = s.run("BeforeSmoke")
    assert result["observations"]["declared_env_removals"]["f42-agent"][VERTEX_NAMES[0]] == "still_present"
    s.world.set_env(CAND_REV["f42-agent"], VERTEX_NAMES[0], SENTINEL + "-changed")
    error = s.stop("BeforeSmoke")
    assert SENTINEL not in error.message
    assert SENTINEL not in capsys.readouterr().out
    for file in tmp_path.rglob("*"):
        if file.is_file():
            assert SENTINEL.encode() not in file.read_bytes(), file
    assert json.loads((s.release_dir / "readbacks" / "BeforeSmoke-01.json").read_text(encoding="utf-8"))["observations"]
