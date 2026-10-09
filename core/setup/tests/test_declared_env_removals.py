"""Release A removes two of f42-agent's environment variables. They are on every live revision and deploy_flags.env does not
set them. The deploy removes exactly the live names whose SHA-256 is in the declared table, on f42-agent only, and the
preservation readback expects them gone and treats anything else that changes as a stop. The names are kept out of the
repository: the engine holds the SHA-256 of each.

Every behaviour test here runs on two synthetic names whose digests are injected into the table, so the logic is exercised
without the real names. Only test_the_two_digests_are_the_sha256_of_the_real_names reads the names file outside the
repository, and it says why it is skipped when the file is absent. The readback records names and the words removed or
still_present, never a value.
"""
import hashlib
import json
import re

import pytest

from core.setup.release import declared_env_removals as declared
from core.setup.tests.release_world import (AGENT_ENV, CAND_REV, REAL_NAMES, SYNTHETIC_DECLARED, Scenario, World,
                                            synthetic_digests)

SENTINEL = "SENTINEL-DECLARED-VALUE-9d41"
NAMES = SYNTHETIC_DECLARED
DIGESTS = tuple(sorted(hashlib.sha256(name.encode("utf-8")).hexdigest() for name in NAMES))
needs_names = pytest.mark.skipif(
    len(REAL_NAMES) != 2,
    reason="the declared names file (wave8/release-packet/declared-env-removals.txt, or F42_DECLARED_ENV_NAMES_FILE) is absent; "
           "the two names are kept out of the repository")


@pytest.fixture(autouse=True)
def synthetic_table(monkeypatch):
    monkeypatch.setattr(declared, "DECLARED_ENV_REMOVAL_DIGESTS", synthetic_digests())


def declared_world():
    return World(declared=NAMES)


def agent_candidate(tmp_path, *, remove=NAMES, override=None):
    s = Scenario(tmp_path, world=declared_world())
    s.to("BeforeCandidate")
    s.world.deploy_candidate("f42-agent", remove_env=remove, env_override=override)
    s.world.deploy_candidate("f42-api")
    return s


def test_the_real_declaration_is_two_digests_for_the_agent_and_nothing_for_any_other_service(monkeypatch):
    monkeypatch.undo()
    assert set(declared.DECLARED_ENV_REMOVAL_DIGESTS) == {"f42-agent"}
    digests = declared.DECLARED_ENV_REMOVAL_DIGESTS["f42-agent"]
    assert len(digests) == 2 and len(set(digests)) == 2
    assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in digests)


@needs_names
def test_the_two_digests_are_the_sha256_of_the_real_names(monkeypatch):
    monkeypatch.undo()
    assert {hashlib.sha256(name.encode("utf-8")).hexdigest() for name in REAL_NAMES} == set(declared.DECLARED_ENV_REMOVAL_DIGESTS["f42-agent"])
    for name in REAL_NAMES:
        assert declared.declared_removal("f42-agent", name) is True and declared.declared_removal("f42-api", name) is False
        for module in (declared, __import__("core.setup.release.services_only", fromlist=["x"])):
            assert name.lower() not in open(module.__file__, encoding="utf-8").read().lower()


def test_a_name_is_declared_when_its_digest_is_listed_for_that_service_and_not_otherwise():
    for name in NAMES:
        assert declared.declared_removal("f42-agent", name) is True
        assert declared.declared_removal("f42-api", name) is False
    for name in ("F42_DATA", "", NAMES[0].lower(), NAMES[0] + " "):
        assert declared.declared_removal("f42-agent", name) is False


def test_the_candidate_that_lost_both_declared_variables_passes_and_the_readback_says_removed(tmp_path):
    s = agent_candidate(tmp_path)
    result = s.run("BeforeSmoke")
    assert result["observations"]["declared_env_removals"]["f42-agent"] == {digest: "removed" for digest in DIGESTS}


def test_the_candidate_that_kept_both_unchanged_is_a_stop_because_the_release_expects_them_removed(tmp_path):
    s = agent_candidate(tmp_path, remove=())
    assert s.stop("BeforeSmoke").code == "ENV"


@pytest.mark.parametrize("index", [0, 1])
def test_a_candidate_that_lost_only_one_of_them_is_a_stop(tmp_path, index):
    s = agent_candidate(tmp_path, remove=(NAMES[index],))
    assert s.stop("BeforeSmoke").code == "ENV"


@pytest.mark.parametrize("index", [0, 1])
def test_a_declared_variable_that_is_still_there_with_another_value_is_a_stop(tmp_path, index):
    s = agent_candidate(tmp_path, override={NAMES[index]: "another-value"})
    assert s.stop("BeforeSmoke").code == "ENV"


@pytest.mark.parametrize("name", ["F42_DATA", "GEMINI_MODEL", "F42_T2_READY", "MODEL_PROVIDER"])
def test_removing_any_other_variable_is_still_a_stop_even_alongside_the_declared_removals(tmp_path, name):
    s = agent_candidate(tmp_path, remove=(*NAMES, name))
    assert s.stop("BeforeSmoke").code == "ENV"


def test_an_added_variable_next_to_the_declared_removals_is_a_stop(tmp_path):
    s = agent_candidate(tmp_path, override={"EXTRA_SETTING": "x"})
    assert s.stop("BeforeSmoke").code == "ENV"


def test_the_declaration_does_not_cover_the_api_service(tmp_path):
    world = declared_world()
    for name in NAMES:
        world.set_env("f42-api-00041-lns", name, "1")
        world.svc["f42-api"]["env"][name] = "1"
    s = Scenario(tmp_path, world=world)
    s.to("BeforeCandidate")
    s.world.deploy_candidate("f42-agent", remove_env=NAMES)
    s.world.deploy_candidate("f42-api", remove_env=NAMES)
    assert s.stop("BeforeSmoke").code == "ENV"


def test_the_declared_removal_holds_through_every_candidate_phase(tmp_path):
    s = agent_candidate(tmp_path)
    for phase in ("BeforeSmoke", "AfterSmoke", "BeforePromotion", "AfterAgentPromotion", "AfterPromotion"):
        if phase != "BeforeSmoke":
            s.step(phase)
        assert s.run(phase)["phase"] == phase


def test_a_template_that_still_carries_the_variables_after_the_candidate_deploy_is_a_stop(tmp_path):
    s = agent_candidate(tmp_path)
    s.world.svc["f42-agent"]["env"].update({name: AGENT_ENV.get(name, "ogilvy-trends-v2") for name in NAMES})
    assert s.stop("BeforeSmoke").code in ("PRESERVATION", "ENV")


def test_the_manifest_records_the_names_and_an_expected_hash_that_does_not_depend_on_the_outcome(tmp_path):
    s = Scenario(tmp_path, world=declared_world())
    s.to("Freeze")
    candidate = s.manifest()["candidates"]["f42-agent"]
    assert candidate["env_removals_declared_sha256"] == list(DIGESTS)
    assert s.manifest()["candidates"]["f42-api"]["env_removals_declared_sha256"] == []
    again = Scenario(tmp_path / "again", world=declared_world())
    again.to("Freeze")
    assert again.manifest()["candidates"]["f42-agent"]["expected_env_sha256"] == candidate["expected_env_sha256"]


def test_a_baseline_without_the_declared_variables_has_nothing_to_remove(tmp_path):
    s = Scenario(tmp_path)
    s.to("BeforeCandidate")
    s.world.deploy_candidate("f42-agent")
    s.world.deploy_candidate("f42-api")
    assert s.manifest()["candidates"]["f42-agent"]["env_removals_declared_sha256"] == []
    assert "declared_env_removals" not in s.run("BeforeSmoke")["observations"]


def test_with_no_synthetic_digests_injected_the_same_names_are_ordinary_variables(tmp_path, monkeypatch):
    monkeypatch.undo()
    s = Scenario(tmp_path, world=declared_world())
    s.to("BeforeCandidate")
    s.world.deploy_candidate("f42-agent", remove_env=NAMES)
    s.world.deploy_candidate("f42-api")
    assert s.manifest()["candidates"]["f42-agent"]["env_removals_declared_sha256"] == []
    assert s.stop("BeforeSmoke").code == "ENV"


def test_no_value_of_a_declared_variable_reaches_an_output_or_a_file(tmp_path, capsys):
    world = declared_world()
    for name in NAMES:
        world.set_env("f42-agent-00047-677", name, SENTINEL)
        world.svc["f42-agent"]["env"][name] = SENTINEL
    s = Scenario(tmp_path, world=world)
    s.to("BeforeCandidate")
    world.deploy_candidate("f42-agent", remove_env=NAMES)
    world.deploy_candidate("f42-api")
    result = s.run("BeforeSmoke")
    assert result["observations"]["declared_env_removals"]["f42-agent"][hashlib.sha256(NAMES[0].encode("utf-8")).hexdigest()] == "removed"
    s.world.set_env(CAND_REV["f42-agent"], NAMES[0], SENTINEL + "-changed")
    error = s.stop("BeforeSmoke")
    assert SENTINEL not in error.message
    assert SENTINEL not in capsys.readouterr().out
    for file in tmp_path.rglob("*"):
        if file.is_file():
            assert SENTINEL.encode() not in file.read_bytes(), file
    assert json.loads((s.release_dir / "readbacks" / "BeforeSmoke-01.json").read_text(encoding="utf-8"))["observations"]


# The live read the paste shows the operator and the deploy script acts on: one function, one table.

def description(*names):
    """What gcloud run services describe --format=json returns for a service, reduced to the env names."""
    return {"spec": {"template": {"spec": {"containers": [{"env": [{"name": n, "value": "x"} for n in names]}]}}}}


def test_live_env_names_are_the_names_on_the_template_containers():
    assert declared.live_env_names(description("A", "B")) == ["A", "B"]
    assert declared.live_env_names({}) == []
    assert declared.live_env_names({"spec": {"template": {"spec": {"containers": [{}]}}}}) == []


def test_declared_live_names_are_only_the_listed_ones_that_are_live_for_that_service():
    live = description("F42_DATA", *NAMES, "GEMINI_MODEL")
    assert declared.declared_live_names("f42-agent", live) == sorted(NAMES)
    assert declared.declared_live_names("f42-api", live) == []
    assert declared.declared_live_names("f42-agent", description("F42_DATA")) == []
    assert declared.declared_live_names("f42-agent", description(NAMES[1])) == [NAMES[1]]


def run_cli(capsys, monkeypatch, text, *argv):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    code = declared.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_the_command_line_reads_a_describe_on_stdin_and_prints_one_matched_name_per_line(capsys, monkeypatch):
    code, out, err = run_cli(capsys, monkeypatch, json.dumps(description("F42_DATA", NAMES[1], NAMES[0])), "--service", "f42-agent")
    assert code == 0 and out.splitlines() == sorted(NAMES) and err == ""


def test_the_command_line_prints_nothing_when_no_declared_name_is_live(capsys, monkeypatch):
    code, out, _ = run_cli(capsys, monkeypatch, json.dumps(description("F42_DATA")), "--service", "f42-agent")
    assert code == 0 and out == ""


def test_the_command_line_refuses_input_that_is_not_json_or_a_service_it_does_not_know(capsys, monkeypatch):
    code, out, err = run_cli(capsys, monkeypatch, "not json", "--service", "f42-agent")
    assert code != 0 and out == "" and "not JSON" in err
    code, out, err = run_cli(capsys, monkeypatch, "{}", "--service", "other")
    assert code != 0 and out == ""


def test_a_matched_name_that_is_not_a_plain_identifier_is_refused_and_never_printed(capsys, monkeypatch):
    bad = "BAD NAME;touch x"
    monkeypatch.setattr(declared, "DECLARED_ENV_REMOVAL_DIGESTS", {"f42-agent": (hashlib.sha256(bad.encode("utf-8")).hexdigest(),)})
    code, out, err = run_cli(capsys, monkeypatch, json.dumps(description(bad)), "--service", "f42-agent")
    assert code != 0 and bad not in out and bad not in err


def test_no_declared_name_is_written_to_the_manifest_or_to_any_readback_only_its_digest(tmp_path):
    s = agent_candidate(tmp_path)
    for phase in ("BeforeSmoke", "AfterSmoke", "BeforePromotion", "AfterAgentPromotion", "AfterPromotion"):
        if phase != "BeforeSmoke":
            s.step(phase)
        s.run(phase)
    written = [p for p in (list(s.release_dir.rglob("*.json")) + list(s.release_dir.rglob("*.sha256")) + list(s.evidence.rglob("*")))
               if p.is_file()]
    assert any(p.name == "release-manifest.json" for p in written) and any("BeforeSmoke" in p.name for p in written)
    text = "\n".join(p.read_text(encoding="utf-8") for p in written)
    for name in NAMES:
        assert name not in text and name.lower() not in text.lower()
    for digest in DIGESTS:
        assert digest in text
