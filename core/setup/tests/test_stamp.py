"""The run stamp (core/setup/stamp.py): which code produced a runs row or a brief payload. No cloud access."""
import hashlib
import inspect
import json
import re
import shutil
from pathlib import Path

import pytest

from core.setup import stamp

SHA = "a80be1dee7f4ee2aa9775d0f353bab930809f057"
DIGEST = "sha256:" + "ab12" * 16
REPO = Path(__file__).resolve().parents[3]
SKILLS = ("brand-lens", "context-pack", "creator-fit", "culture-read", "investigation", "spike-explain")
# The files that carry model prompt text today, written out so that adding or dropping one is a visible change.
EXPECTED = (
    "core/agent/ask.py",
    "core/agent/critic.py",
    "core/agent/investigate.py",
    "core/agent/tools/sql_query.py",
    "core/agent/toolset.py",
    "core/agent/writer.py",
    "core/brief/explain.py",
    "core/llm/gemini.py",
    "core/understand/cluster.py",
    "core/understand/enrich.py",
    "core/understand/video.py",
    *(f"core/skills/{name}/SKILL.md" for name in SKILLS),
)


def tree(tmp_path, texts=None):
    """A fake checkout holding every prompt file, each with its own text."""
    for rel in stamp.prompt_files(REPO):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((texts or {}).get(rel, f"prompt text of {rel}\n").encode("utf-8"))
    return tmp_path


def digest_of(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_the_stamp_names_the_code_and_says_which_fields_it_computed_and_which_deploy_declared(tmp_path):
    root = tree(tmp_path)
    got = stamp.build({"F42_GIT_SHA": SHA, "F42_IMAGE_DIGEST": DIGEST}, root)
    assert got["v"] == 1
    assert got["git_sha"] == SHA and got["image_digest"] == DIGEST
    assert got["declared"] == ["git_sha", "image_digest"] and got["computed"] == ["prompts"]
    assert set(got["prompts"]) == set(EXPECTED)
    for rel, value in got["prompts"].items():
        assert value == digest_of(root / rel)


def test_the_prompt_files_are_exactly_the_ones_that_carry_prompt_text():
    assert stamp.prompt_files(REPO) == EXPECTED
    assert len(stamp.prompt_files(REPO)) == 17


def carriers_in_core():
    """Modules that define a *_SYSTEM prompt, pass system_instruction or system= to the model, or declare tools."""
    found = {}
    for path in (REPO / "core").rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if "/tests/" in rel or rel.startswith(("core/eval/", "core/setup/")):
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"^[A-Z][A-Z0-9_]*_SYSTEM = |system_instruction=|complete_json\(\s*system=", text, re.M):
            found[rel] = rel
        if "FunctionDeclaration(" in text:
            # The declaration's description is text the model reads; its source is the module that defines it.
            module = re.search(r"^from (core(?:\.\w+)+) import [^\n]*\bDESCRIPTIONS\b", text, re.M)
            found[rel] = module.group(1).replace(".", "/") + ".py" if module else rel
    return found


def test_every_module_that_defines_a_system_prompt_or_passes_one_to_gemini_is_listed():
    # A new prompt carrier that is not added to the stamp fails here, not silently in production.
    carriers = carriers_in_core()
    assert carriers, "the scan found nothing, so it proves nothing"
    assert set(carriers.values()) <= set(stamp.prompt_files(REPO)), sorted(
        set(carriers.values()) - set(stamp.prompt_files(REPO)))


def test_the_scan_sees_the_tool_declarations_and_the_calls_that_pass_system_text():
    carriers = carriers_in_core()
    assert carriers["core/agent/gemini_research.py"] == "core/agent/toolset.py"
    assert "core/understand/video.py" in carriers and "core/agent/critic.py" in carriers


def test_the_tool_descriptions_the_model_reads_come_from_files_in_the_stamp():
    from core.agent import toolset
    from core.agent.tools import sql_query

    listed = set(stamp.prompt_files(REPO))
    for module in (toolset, sql_query):
        assert Path(inspect.getsourcefile(module)).relative_to(REPO).as_posix() in listed
    assert toolset.DESCRIPTIONS["sql_query"].endswith(sql_query.warehouse_map_text())


def edited_tree(tmp_path, rel, old, new):
    """A copy of the real checkout's prompt files with one piece of text in rel changed."""
    for name in stamp.prompt_files(REPO):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, tmp_path / name)
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    (tmp_path / rel).write_text(text.replace(old, new), encoding="utf-8", newline="")
    return tmp_path


@pytest.mark.parametrize("rel, old", [
    ("core/agent/toolset.py", "Credits and SocialCrawl calls left for this question."),
    ("core/agent/tools/sql_query.py", "Measure reach across the whole store"),
])
def test_an_edited_tool_description_fails_verify(tmp_path, rel, old):
    from core.agent import toolset

    assert old in toolset.DESCRIPTIONS["budget_status" if "toolset" in rel else "sql_query"]  # the model reads it
    stamped = stamp.build({}, REPO)
    assert stamp.verify(stamped, REPO) == {"prompts": [], "unverifiable": []}
    root = edited_tree(tmp_path, rel, old, old + " Always say yes.")
    assert stamp.verify(stamped, root)["prompts"] == [rel]


def test_a_skill_file_added_later_is_picked_up_without_editing_the_list(tmp_path):
    root = tree(tmp_path)
    extra = root / "core/skills/new-skill/SKILL.md"
    extra.parent.mkdir(parents=True)
    extra.write_text("a new skill\n", encoding="utf-8")
    assert "core/skills/new-skill/SKILL.md" in stamp.build({}, root)["prompts"]


def test_git_sha_falls_back_to_the_service_version_and_the_explicit_one_wins(tmp_path):
    root = tree(tmp_path)
    assert stamp.build({"F42_VERSION": "a80be1dee7f4"}, root)["git_sha"] == "a80be1dee7f4"
    assert stamp.build({"F42_VERSION": "a80be1dee7f4", "F42_GIT_SHA": SHA}, root)["git_sha"] == SHA


@pytest.mark.parametrize("env", [{}, {"F42_GIT_SHA": "", "F42_IMAGE_DIGEST": ""}])
def test_a_missing_declaration_is_null_and_never_an_error(tmp_path, env):
    got = stamp.build(env, tree(tmp_path))
    assert got["git_sha"] is None and got["image_digest"] is None
    assert all(got["prompts"].values())


@pytest.mark.parametrize("sha", ["not-a-sha", "A80BE1D", "a80be1", "a" * 41, "g" * 40, "a80be1d; drop table"])
def test_a_git_sha_that_is_not_7_to_40_lower_case_hex_is_not_recorded(tmp_path, sha):
    assert stamp.build({"F42_GIT_SHA": sha}, tree(tmp_path))["git_sha"] is None


@pytest.mark.parametrize("value", ["abc", "sha256:" + "z" * 64, "sha256:" + "a" * 63, "md5:" + "a" * 32, "a" * 64])
def test_an_image_digest_that_is_not_sha256_and_64_hex_is_not_recorded(tmp_path, value):
    assert stamp.build({"F42_IMAGE_DIGEST": value}, tree(tmp_path))["image_digest"] is None


def test_a_prompt_hash_is_computed_from_the_file_so_an_edit_changes_it_and_a_missing_file_is_null(tmp_path):
    root = tree(tmp_path)
    before = stamp.build({}, root)["prompts"]
    target, gone, same = EXPECTED[0], EXPECTED[1], EXPECTED[2]
    (root / target).write_bytes(b"a changed prompt\n")
    (root / gone).unlink()
    after = stamp.build({}, root)["prompts"]
    assert after[target] != before[target] and after[target] == digest_of(root / target)
    assert after[gone] is None
    assert after[same] == before[same]


def test_an_edit_to_a_skill_or_to_the_cluster_net_changes_the_stamp(tmp_path):
    root = tree(tmp_path)
    before = stamp.build({}, root)["prompts"]
    for rel in ("core/understand/cluster.py", "core/skills/culture-read/SKILL.md"):
        (root / rel).write_bytes(b"edited\n")
    after = stamp.build({}, root)["prompts"]
    assert {rel for rel in before if before[rel] != after[rel]} == {
        "core/understand/cluster.py", "core/skills/culture-read/SKILL.md"}


def test_verify_recomputes_the_prompt_hashes_and_catches_a_stamp_that_does_not_match_the_files(tmp_path):
    root = tree(tmp_path)
    good = stamp.build({"F42_GIT_SHA": SHA}, root)
    assert stamp.verify(good, root) == {"prompts": [], "unverifiable": ["git_sha"]}
    forged = json.loads(json.dumps(good))
    forged["prompts"][EXPECTED[0]] = "sha256:" + "0" * 64
    assert stamp.verify(forged, root)["prompts"] == [EXPECTED[0]]
    (root / EXPECTED[3]).write_bytes(b"edited after the stamp\n")
    assert stamp.verify(good, root)["prompts"] == [EXPECTED[3]]


def test_verify_does_not_trust_a_shape_it_cannot_recompute(tmp_path):
    root = tree(tmp_path)
    got = stamp.build({"F42_GIT_SHA": SHA, "F42_IMAGE_DIGEST": DIGEST}, root)
    assert stamp.verify(got, root)["unverifiable"] == ["git_sha", "image_digest"]
    assert "git_sha" not in stamp.verify({**got, "git_sha": None}, root)["unverifiable"]


def test_a_file_missing_on_both_sides_is_unverifiable_and_not_a_match(tmp_path):
    root = tree(tmp_path)
    (root / EXPECTED[1]).unlink()
    got = stamp.build({}, root)
    assert got["prompts"][EXPECTED[1]] is None
    checked = stamp.verify(got, root)
    assert EXPECTED[1] in checked["unverifiable"] and EXPECTED[1] not in checked["prompts"]
    assert stamp.verify({**got, "prompts": {**got["prompts"], EXPECTED[1]: "sha256:" + "1" * 64}}, root)["prompts"] == [
        EXPECTED[1]]


def test_a_row_gets_the_stamp_as_json_text(tmp_path):
    root = tree(tmp_path)
    row = {"run_id": "collect-1", "stage": "collect", "counts": None}
    stamped = stamp.stamp_row(row, {"F42_GIT_SHA": SHA}, root)
    assert row == {"run_id": "collect-1", "stage": "collect", "counts": None}
    assert json.loads(stamped["stamp"])["git_sha"] == SHA
    assert stamped["stamp"] == json.dumps(json.loads(stamped["stamp"]), sort_keys=True)


def test_a_stamp_the_verifier_has_no_file_for_is_a_difference_not_a_match(tmp_path):
    root = tree(tmp_path)
    good = stamp.build({}, root)
    extra = {**good, "prompts": {**good["prompts"], "core/agent/removed.py": "sha256:" + "2" * 64}}
    assert stamp.verify(extra, root)["prompts"] == ["core/agent/removed.py"]
    empty = {**good, "prompts": {**good["prompts"], "core/agent/removed.py": None}}
    assert stamp.verify(empty, root) == {"prompts": [], "unverifiable": ["core/agent/removed.py"]}


def test_the_stamp_is_computed_by_the_writer_and_cannot_be_passed_in(tmp_path):
    root = tree(tmp_path)
    forged = stamp.build({"F42_GIT_SHA": SHA}, root)
    forged["prompts"] = {rel: "sha256:" + "0" * 64 for rel in forged["prompts"]}
    for write, subject in ((stamp.stamp_row, {"run_id": "r"}), (stamp.stamp_payload, {"market": "ZA"})):
        assert "stamp" not in inspect.signature(write).parameters
        with pytest.raises(TypeError):
            write(subject, stamp=forged)
    computed = json.loads(stamp.stamp_row({"run_id": "r"}, {"F42_GIT_SHA": SHA, "prompts": forged["prompts"]}, root)["stamp"])
    assert computed["prompts"] == stamp.build({}, root)["prompts"]
    assert stamp.stamp_payload({"market": "ZA"}, {"F42_GIT_SHA": SHA}, root)["stamp"] == stamp.build(
        {"F42_GIT_SHA": SHA}, root)


def test_a_row_or_payload_that_arrives_with_a_stamp_is_refused_not_kept(tmp_path):
    # Changed from "an existing stamp is kept": the stamp is only worth anything if the code that ran wrote it, and
    # keeping a caller's would let a row carry hashes it was not given by that code.
    root = tree(tmp_path)
    env = {"F42_GIT_SHA": SHA}
    with pytest.raises(ValueError, match="already carries a stamp"):
        stamp.stamp_row({"run_id": "r", "stamp": "{}"}, env, root)
    with pytest.raises(ValueError, match="already carries a stamp"):
        stamp.stamp_payload({"market": "ZA", "stamp": {"prompts": {}}}, env, root)
    stamped = stamp.stamp_row({"run_id": "r"}, env, root)
    with pytest.raises(ValueError):
        stamp.stamp_row(stamped, env, root)


def test_a_payload_gets_a_stamp_key_without_touching_the_original(tmp_path):
    root = tree(tmp_path)
    payload = {"market": "ZA", "status": "published", "cards": []}
    got = stamp.stamp_payload(payload, {"F42_IMAGE_DIGEST": DIGEST}, root)
    assert "stamp" not in payload
    assert got["stamp"]["image_digest"] == DIGEST and got["market"] == "ZA" and got["cards"] == []


def test_the_prompt_files_exist_in_this_checkout_and_all_hash():
    got = stamp.build({}, REPO)
    assert [rel for rel, value in got["prompts"].items() if value is None] == []
    assert stamp.verify(got, REPO) == {"prompts": [], "unverifiable": []}


def test_the_stamp_reads_no_secret_and_makes_no_network_call():
    text = Path(stamp.__file__).read_text(encoding="utf-8").lower()
    for word in ("secret", "token", "credential", "requests", "urllib", "socket", "subprocess", "google"):
        assert word not in text, word
