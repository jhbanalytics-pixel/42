"""A packet made only by the packet command line, read by the real release paste (SERVICES-PASTE.ps1) as a pwsh process with the
commands doubled. The packet files are not built by hand here: every one of the five comes out of core.setup.release.packet, and the
paste's own validation (Test-Packet, Test-Receipt) is what accepts or refuses them. Like the other paste tests, a missing pwsh fails."""
import shutil

import pytest

from core.setup.release import packet
from core.setup.release import services_only as so
from core.setup.tests import packet_world as pw
from core.setup.tests import release_world as rw
from core.setup.tests.paste_world import PasteWorld

CANDIDATE_STEPS = ["helper-BeforeAnyWrite-0", "archive", "extract", "build", "helper-Freeze-0", "durable-validate", "pin-f42-agent",
                   "pin-f42-api", "helper-BeforeCandidate-0", "deploy-candidate", "helper-BeforeSmoke-0", "smoke", "helper-AfterSmoke-0"]


@pytest.fixture(autouse=True)
def pwsh_is_present():
    if shutil.which("pwsh") is None:
        pytest.fail("pwsh is not installed; the paste tests cannot run (install PowerShell 7)")


def make_packet(tmp_path, monkeypatch, capsys, action):
    """Every packet file from the packet command line, the repository and the paste copied from the paste test world."""
    world = PasteWorld(tmp_path / "paste", action)
    bench = pw.Bench(tmp_path / "bench", monkeypatch, repo=world.repo)
    bench.lock_dir = tmp_path / "locks"
    bench.prepare_inputs()
    for argv in (bench.capture_argv(), bench.bind_argv(), bench.lock_argv(), bench.review_argv(), bench.receipt_argv()):
        code = packet.main(argv)
        out = capsys.readouterr()
        assert code == 0, (argv[0], out.err)
    world.lock, world.review, world.bindings, world.receipt = bench.lock_file(), bench.review, bench.bindings, bench.receipt
    world.release_dir = bench.packet
    world.paths = {"manifest": bench.packet / "release-manifest.json", "sidecar": bench.packet / "release-manifest.sha256",
                   "smoke": bench.packet / "smoke-receipt.json"}
    tag_url = so.tag_url(bench.release_id, bench.baseline_value()["canonical"]["f42-api"])
    extra = {"commit": bench.target, "tree": pw.git(bench.repo, "rev-parse", "HEAD^{tree}"), "api_tag_url": tag_url}
    return world, bench, extra


def test_a_packet_made_by_the_command_line_is_accepted_by_the_paste_checks_and_candidate_runs_its_steps(tmp_path, monkeypatch, capsys):
    world, bench, extra = make_packet(tmp_path, monkeypatch, capsys, "Candidate")
    result = world.run(extra=extra)
    assert result.returncode == 0, result.stderr
    assert result.external[0]["kind"] == "read"
    assert result.names == CANDIDATE_STEPS
    assert "NOT EXECUTABLE" not in result.stderr


def test_the_same_packet_lets_rollback_restore_the_revisions_the_captured_baseline_names(tmp_path, monkeypatch, capsys):
    world, bench, extra = make_packet(tmp_path, monkeypatch, capsys, "Rollback")
    result = world.run(extra=extra)
    assert result.returncode == 0, result.stderr
    assert result.names == ["helper-BeforeRollback-0", "restore-f42-api", "restore-f42-agent", "helper-AfterRollback-0"]
    assert f"--to-revisions={rw.A80_REV['f42-api']}=100" in result.run("restore-f42-api")["argv"]
    assert f"--to-revisions={rw.A80_REV['f42-agent']}=100" in result.run("restore-f42-agent")["argv"]


def test_the_same_packet_lets_promote_move_the_candidates_named_by_its_release_id(tmp_path, monkeypatch, capsys):
    world, bench, extra = make_packet(tmp_path, monkeypatch, capsys, "Promote")
    world.after_smoke()
    result = world.run(extra=extra)
    assert result.returncode == 0, result.stderr
    assert f"--to-revisions=f42-agent-{bench.release_id}=100" in result.run("promote-f42-agent")["argv"]
    assert f"--to-revisions=f42-api-{bench.release_id}=100" in result.run("promote-f42-api")["argv"]


@pytest.mark.parametrize("victim", ["bindings", "baseline", "durable", "compat", "old-reader"])
def test_a_change_to_any_file_the_command_line_bound_stops_the_paste_before_its_first_external_call(tmp_path, monkeypatch, capsys, victim):
    world, bench, extra = make_packet(tmp_path, monkeypatch, capsys, "Candidate")
    path = {"bindings": bench.bindings, "baseline": bench.baseline, "durable": bench.durable,
            "compat": bench.packet / "compat-receipt.json", "old-reader": bench.packet / "old-reader-receipt.json"}[victim]
    path.write_bytes(path.read_bytes() + b" ")
    result = world.run(extra=extra)
    assert result.returncode != 0 and result.external == [] and "NOT EXECUTABLE" in result.stderr, victim


def test_a_receipt_from_the_command_line_that_is_edited_to_allow_two_asks_is_refused_by_the_paste(tmp_path, monkeypatch, capsys):
    world, bench, extra = make_packet(tmp_path, monkeypatch, capsys, "Candidate")
    receipt = bench.receipt.read_text(encoding="utf-8").replace('"max_live_asks": 1', '"max_live_asks": 2')
    bench.receipt.write_text(receipt, encoding="utf-8")
    result = world.run(extra=extra)
    assert result.returncode != 0 and result.external == [] and "exactly one live Ask" in result.stderr
