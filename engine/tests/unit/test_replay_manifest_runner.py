"""The replay manifest runner evaluates authorized retained inputs into one receipt."""

from __future__ import annotations

import json
import socket
import subprocess

import pytest
from scripts.staging import run_replay_manifest as runner

from tests.unit.test_open_intelligence_replay import (
    bundle_payload,
    bytes_digest,
    manifest_signals,
    second_retained_capture,
)

BUILD = "0123456789abcdef0123456789abcdef01234567"
OTHER_BUILD = "769408fc55680ca9d920a1e94dacaddba2c0bb91"
REAL_CHECKOUT_STATE = runner.checkout_state
REAL_HIDDEN_INDEX_ENTRIES = runner.hidden_index_entries


@pytest.fixture(autouse=True)
def clean_checkout(monkeypatch):
    """The checkout reader, replaced with a clean checkout at BUILD.

    The tests of the real reader and of a dirty tree set their own state.
    """
    monkeypatch.setattr(runner, "checkout_state", lambda: (BUILD, ""))
    monkeypatch.setattr(runner, "hidden_index_entries", lambda: ())


def signal_rows(source_time: str = "2026-08-25T12:00:00Z") -> list[dict]:
    return [
        {
            "signal_id": item.signal_id,
            "market": item.market,
            "cluster_signature": item.cluster_signature,
            "member_identities": list(item.member_identities),
            "source_max_observed_at": source_time,
            "membership_complete": item.membership_complete,
            "evidence_ready": item.evidence_ready,
            "geo_proven": item.geo_proven,
        }
        for item in manifest_signals()
    ]


def write_bundle(tmp_path, name, payload, *, digest=None, signals=None):
    (tmp_path / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / f"{name}_signals.json").write_text(
        json.dumps(signal_rows() if signals is None else signals), encoding="utf-8"
    )
    return {
        "input_path": f"{name}.json",
        "expected_input_sha256": digest or bytes_digest(payload),
        "signals_path": f"{name}_signals.json",
        "sample_per_market": 1,
    }


def write_manifest(tmp_path, entries):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"bundles": entries}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_two_authorized_cutoffs_write_one_complete_receipt(tmp_path):
    entries = [
        write_bundle(tmp_path, "second", second_retained_capture()),
        write_bundle(tmp_path, "first", bundle_payload()),
    ]
    output = tmp_path / "receipt.json"
    code = runner.main(
        [
            "--manifest",
            str(write_manifest(tmp_path, entries)),
            "--build-version",
            BUILD,
            "--output",
            str(output),
        ]
    )
    assert code == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["build_version"] == BUILD
    assert (receipt["bundle_count"], receipt["evaluated_count"], receipt["refused_count"]) == (
        2,
        2,
        0,
    )
    assert [item["cutoff"] for item in receipt["cutoffs"]] == ["2026-08-25", "2026-09-01"]
    first, second = receipt["cutoffs"]
    assert first["input_sha256"] == bytes_digest(bundle_payload())
    assert (first["counts"]["event_ledger_rows"], second["counts"]["event_ledger_rows"]) == (1, 2)
    assert second["counts"]["row_counts_by_table"]["event_ledger"] == 2
    assert second["counts"]["signals"] == 3
    assert second["known_event_set"]["digest"] == bundle_payload()["known_event_set"]["digest"]
    assert second["known_event_recall_by_market"] == {
        market: {"numerator": 1, "denominator": 1, "rate": 1.0} for market in ("ke", "ng", "za")
    }
    proof = second["membership_proof"]
    assert proof["receipt_completeness"] == {"numerator": 3, "denominator": 3, "rate": 1.0}
    assert proof["source_digest"] == second_retained_capture()["source_snapshot"]["source_digest"]
    assert len(proof["receipts_by_member_sha256"]) == 64
    assert set(second["exclusions"]) <= {"ke", "ng", "za"}
    for market in second["exclusions"].values():
        assert set(market) == {"state", "gaps", "excluded_rows", "unavailable_rows"}
    unsealed = dict(receipt)
    assert unsealed.pop("receipt_sha256") == runner._digest(unsealed)


def test_a_bundle_whose_authority_fails_is_refused_and_the_run_exits_nonzero(tmp_path):
    rebound = second_retained_capture()
    rebound["source_snapshot"]["row_counts_by_table"]["event_ledger"] = 3
    entries = [
        write_bundle(tmp_path, "rebound", rebound),
        write_bundle(tmp_path, "first", bundle_payload()),
    ]
    output = tmp_path / "receipt.json"
    code = runner.main(
        [
            "--manifest",
            str(write_manifest(tmp_path, entries)),
            "--build-version",
            BUILD,
            "--output",
            str(output),
        ]
    )
    assert code == 1
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert [item["cutoff"] for item in receipt["cutoffs"]] == ["2026-08-25"]
    assert receipt["refused"] == [
        {
            "index": 0,
            "input_path": "rebound.json",
            "reason": "IncompleteReplayInput",
            "detail": "event_ledger row count is inconsistent",
        }
    ]


def test_a_mutated_input_under_its_old_digest_is_refused(tmp_path):
    payload = second_retained_capture()
    digest = bytes_digest(payload)
    payload["source_snapshot"]["row_counts_by_table"]["event_ledger"] = 3
    entry = write_bundle(tmp_path, "mutated", payload, digest=digest)
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["evaluated_count"] == 0
    assert receipt["refused"][0]["detail"] == "replay_input_digest_mismatch"


def test_future_signals_and_duplicate_cutoffs_are_refused(tmp_path):
    entries = [
        write_bundle(tmp_path, "first", bundle_payload()),
        write_bundle(tmp_path, "again", bundle_payload()),
        write_bundle(
            tmp_path,
            "future",
            second_retained_capture(),
            signals=signal_rows("2026-09-02T00:00:00Z"),
        ),
    ]
    receipt = runner.run_replay_manifest(
        {"bundles": entries}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["evaluated_count"] == 1
    assert [(item["index"], item["detail"]) for item in receipt["refused"]] == [
        (1, "duplicate_cutoff"),
        (2, "future source timestamp for sig_ke"),
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda entry: entry.pop("sample_per_market"),
        lambda entry: entry.update(expected_input_sha256="abc"),
        lambda entry: entry.update(sample_per_market=0),
        lambda entry: entry.update(extra=True),
    ],
)
def test_malformed_bundle_entries_are_refused(tmp_path, mutation):
    entry = write_bundle(tmp_path, "first", bundle_payload())
    mutation(entry)
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["evaluated_count"] == 0
    assert receipt["refused_count"] == 1


def test_signal_rows_with_extra_or_missing_fields_are_refused(tmp_path):
    rows = signal_rows()
    rows[0]["extra"] = 1
    entry = write_bundle(tmp_path, "first", bundle_payload(), signals=rows)
    receipt = runner.run_replay_manifest(
        {"bundles": [entry]}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["refused"][0]["detail"] == "signal_fields_invalid"


def test_existing_receipt_is_never_overwritten_and_bad_build_version_refuses(tmp_path):
    entries = [write_bundle(tmp_path, "first", bundle_payload())]
    manifest = str(write_manifest(tmp_path, entries))
    output = tmp_path / "receipt.json"
    output.write_text("kept", encoding="utf-8")
    assert (
        runner.main(["--manifest", manifest, "--build-version", BUILD, "--output", str(output)])
        == 2
    )
    assert output.read_text(encoding="utf-8") == "kept"
    assert (
        runner.main(
            [
                "--manifest",
                manifest,
                "--build-version",
                "HEAD",
                "--output",
                str(tmp_path / "o.json"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "o.json").exists()
    with pytest.raises(runner.ReplayManifestRefusal, match="manifest_bundles_required"):
        runner.run_replay_manifest({"bundles": []}, base_dir=tmp_path, build_version=BUILD)


def test_build_version_must_equal_the_running_checkout_head(tmp_path):
    assert BUILD != OTHER_BUILD
    entries = [write_bundle(tmp_path, "first", bundle_payload())]
    manifest = str(write_manifest(tmp_path, entries))
    output = tmp_path / "receipt.json"
    code = runner.main(
        ["--manifest", manifest, "--build-version", OTHER_BUILD, "--output", str(output)]
    )
    assert code == 2
    assert not output.exists()
    with pytest.raises(runner.ReplayManifestRefusal, match="build_version_differs_from_checkout"):
        runner.run_replay_manifest(
            {"bundles": entries}, base_dir=tmp_path, build_version=OTHER_BUILD
        )


def test_receipt_states_that_admitted_inputs_are_synthetic(tmp_path):
    entries = [write_bundle(tmp_path, "first", bundle_payload())]
    receipt = runner.run_replay_manifest(
        {"bundles": entries}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["checkout_head"] == BUILD
    assert receipt["evidence_scope"] == "synthetic_test_only"
    assert "synthetic_test_only" in receipt["evidence_note"]
    assert "synthetic_test_only" in runner.__doc__


def test_a_checkout_with_uncommitted_changes_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "checkout_state", lambda: (BUILD, " M engine/x.py\n"))
    entries = [write_bundle(tmp_path, "first", bundle_payload())]
    with pytest.raises(runner.ReplayManifestRefusal, match="checkout_not_clean"):
        runner.run_replay_manifest({"bundles": entries}, base_dir=tmp_path, build_version=BUILD)
    output = tmp_path / "receipt.json"
    code = runner.main(
        [
            "--manifest",
            str(write_manifest(tmp_path, entries)),
            "--build-version",
            BUILD,
            "--output",
            str(output),
        ]
    )
    assert code == 2
    assert not output.exists()


def test_receipt_records_the_clean_checkout_check(tmp_path):
    entries = [write_bundle(tmp_path, "first", bundle_payload())]
    receipt = runner.run_replay_manifest(
        {"bundles": entries}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["checkout_clean"] is True
    assert receipt["checkout_status_command"] == (
        "git status --porcelain --untracked-files=all --ignore-submodules=none"
    )


def test_the_real_checkout_reader_returns_head_and_porcelain_status(tmp_path, monkeypatch):
    """The real reader, run over a throwaway repository with one commit.

    The reader's git inherits the process environment, so system and global git config
    are shut out here and the identity is passed to the one commit only. Nothing depends
    on a repository around the engine tree.
    """
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        monkeypatch.delenv(name, raising=False)
    empty_config = tmp_path / "gitconfig"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")

    def git(*arguments):
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "fixture")
    expected_head = git("rev-parse", "HEAD").strip()

    head, status = REAL_CHECKOUT_STATE(root=repo)
    assert head == expected_head
    assert len(head) == 40
    assert all(character in "0123456789abcdef" for character in head)
    assert status == ""

    (repo / "notes").mkdir()
    (repo / "notes" / "stray.txt").write_text("stray\n", encoding="utf-8")
    head, status = REAL_CHECKOUT_STATE(root=repo)
    assert head == expected_head
    assert "notes/stray.txt" in status


@pytest.mark.parametrize(
    "listing",
    [
        "S engine/x.py\n",
        "h engine/x.py\n",
        "s engine/x.py\n",
        "H a.py\nh engine/x.py\n",
    ],
)
def test_skip_worktree_or_assume_unchanged_entries_are_refused(tmp_path, monkeypatch, listing):
    monkeypatch.setattr(
        runner, "hidden_index_entries", lambda: runner.flagged_index_entries(listing)
    )
    entries = [write_bundle(tmp_path, "first", bundle_payload())]
    with pytest.raises(runner.ReplayManifestRefusal, match="checkout_index_flags_hidden_changes"):
        runner.run_replay_manifest({"bundles": entries}, base_dir=tmp_path, build_version=BUILD)


def test_flagged_index_entries_reads_the_ls_files_tags():
    listing = "H a.py\nS b.py\nh c.py\nC d.py\ns e.py\nM f.py\n"
    assert runner.flagged_index_entries(listing) == ("S b.py", "h c.py", "s e.py")
    assert runner.flagged_index_entries("") == ()


def test_receipt_records_the_index_flag_check(tmp_path):
    entries = [write_bundle(tmp_path, "first", bundle_payload())]
    receipt = runner.run_replay_manifest(
        {"bundles": entries}, base_dir=tmp_path, build_version=BUILD
    )
    assert receipt["checkout_index_command"] == ("git ls-files -v --full-name, from the top level")
    assert receipt["checkout_hidden_index_entries"] == 0
    assert "ignored" in runner.__doc__


def test_the_real_hidden_index_reader_returns_a_tuple():
    assert isinstance(REAL_HIDDEN_INDEX_ENTRIES(), tuple)
    assert all(entry[0] == "S" or entry[0].islower() for entry in REAL_HIDDEN_INDEX_ENTRIES())


def git_repo(tmp_path):
    """A throwaway local repository with a root file, an ops file and engine/."""
    repo = tmp_path / "repo"
    (repo / "engine").mkdir(parents=True)
    (repo / "ops").mkdir()
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    (repo / "ops" / "tool.py").write_text("ops\n", encoding="utf-8")
    (repo / "engine" / "core.py").write_text("engine\n", encoding="utf-8")

    def git(*arguments):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "fixture")
    return repo, git


@pytest.mark.parametrize("path", ["README.md", "ops/tool.py", "engine/core.py"])
def test_assume_unchanged_anywhere_in_the_repository_is_found_from_engine(tmp_path, path):
    repo, git = git_repo(tmp_path)
    assert REAL_HIDDEN_INDEX_ENTRIES(repo / "engine") == ()
    git("update-index", "--assume-unchanged", path)
    (repo / path).write_text("edited\n", encoding="utf-8")
    assert REAL_HIDDEN_INDEX_ENTRIES(repo / "engine") == (f"h {path}",)
    assert REAL_CHECKOUT_STATE(repo / "engine")[1] == ""


def test_skip_worktree_outside_engine_is_found_with_its_tag(tmp_path):
    repo, git = git_repo(tmp_path)
    git("update-index", "--skip-worktree", "ops/tool.py")
    assert REAL_HIDDEN_INDEX_ENTRIES(repo / "engine") == ("S ops/tool.py",)


def test_untracked_files_are_seen_whatever_the_local_status_config(tmp_path):
    repo, git = git_repo(tmp_path)
    head, status = REAL_CHECKOUT_STATE(repo / "engine")
    assert len(head) == 40
    assert status == ""
    git("config", "status.showUntrackedFiles", "no")
    (repo / "ops" / "new_dir").mkdir()
    (repo / "ops" / "new_dir" / "stray.py").write_text("x\n", encoding="utf-8")
    _head, status = REAL_CHECKOUT_STATE(repo / "engine")
    assert "?? ops/new_dir/stray.py" in status
