"""The run stamp (core/setup/stamp.py): which code produced a runs row or a brief payload. No cloud access."""
import hashlib
import json
from pathlib import Path

import pytest

from core.setup import stamp

SHA = "a80be1dee7f4ee2aa9775d0f353bab930809f057"
DIGEST = "sha256:" + "ab12" * 16
REPO = Path(__file__).resolve().parents[3]


def tree(tmp_path, texts=None):
    """A fake checkout holding every prompt file, each with its own text."""
    for rel in stamp.PROMPT_FILES:
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
    assert set(got["prompts"]) == set(stamp.PROMPT_FILES)
    for rel, value in got["prompts"].items():
        assert value == digest_of(root / rel)


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
    target = stamp.PROMPT_FILES[0]
    (root / target).write_bytes(b"a changed prompt\n")
    (root / stamp.PROMPT_FILES[1]).unlink()
    after = stamp.build({}, root)["prompts"]
    assert after[target] != before[target] and after[target] == digest_of(root / target)
    assert after[stamp.PROMPT_FILES[1]] is None
    assert after[stamp.PROMPT_FILES[2]] == before[stamp.PROMPT_FILES[2]]


def test_verify_recomputes_the_prompt_hashes_and_catches_a_stamp_that_does_not_match_the_files(tmp_path):
    root = tree(tmp_path)
    good = stamp.build({"F42_GIT_SHA": SHA}, root)
    assert stamp.verify(good, root) == {"prompts": [], "unverifiable": ["git_sha"]}
    forged = json.loads(json.dumps(good))
    forged["prompts"][stamp.PROMPT_FILES[0]] = "sha256:" + "0" * 64
    assert stamp.verify(forged, root)["prompts"] == [stamp.PROMPT_FILES[0]]
    (root / stamp.PROMPT_FILES[3]).write_bytes(b"edited after the stamp\n")
    assert stamp.verify(good, root)["prompts"] == [stamp.PROMPT_FILES[3]]


def test_verify_does_not_trust_a_shape_it_cannot_recompute(tmp_path):
    root = tree(tmp_path)
    got = stamp.build({"F42_GIT_SHA": SHA, "F42_IMAGE_DIGEST": DIGEST}, root)
    assert stamp.verify(got, root)["unverifiable"] == ["git_sha", "image_digest"]
    assert "git_sha" not in stamp.verify({**got, "git_sha": None}, root)["unverifiable"]


def test_a_row_gets_the_stamp_as_json_text_and_an_existing_stamp_is_kept(tmp_path):
    root = tree(tmp_path)
    row = {"run_id": "collect-1", "stage": "collect", "counts": None}
    stamped = stamp.stamp_row(row, stamp.build({"F42_GIT_SHA": SHA}, root))
    assert row == {"run_id": "collect-1", "stage": "collect", "counts": None}
    assert json.loads(stamped["stamp"])["git_sha"] == SHA
    assert stamped["stamp"] == json.dumps(json.loads(stamped["stamp"]), sort_keys=True)
    again = stamp.stamp_row(stamped, stamp.build({"F42_GIT_SHA": "b" * 40}, root))
    assert again["stamp"] == stamped["stamp"]


def test_a_payload_gets_a_stamp_key_without_touching_the_original(tmp_path):
    root = tree(tmp_path)
    payload = {"market": "ZA", "status": "published", "cards": []}
    got = stamp.stamp_payload(payload, stamp.build({"F42_IMAGE_DIGEST": DIGEST}, root))
    assert "stamp" not in payload
    assert got["stamp"]["image_digest"] == DIGEST and got["market"] == "ZA" and got["cards"] == []
    assert stamp.stamp_payload(got, stamp.build({}, root))["stamp"] == got["stamp"]


def test_the_prompt_files_exist_in_this_checkout_and_all_hash():
    got = stamp.build({}, REPO)
    assert [rel for rel, value in got["prompts"].items() if value is None] == []
    assert stamp.verify(got, REPO)["prompts"] == []


def test_the_stamp_reads_no_secret_and_makes_no_network_call():
    text = Path(stamp.__file__).read_text(encoding="utf-8").lower()
    for word in ("secret", "token", "credential", "requests", "urllib", "socket", "subprocess", "google"):
        assert word not in text, word
