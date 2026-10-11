"""The model hold ceiling in the sentence Albert types (review of Release C, finding m6): rounded up to the cent, never down, and read from the
repository and commit the release is bound to, not from whichever checkout runs the packet tool."""
import subprocess
import sys

import pytest

from core.agent import ask
from core.setup.release import packet
from core.setup.tests import packet_world as pw

ROOT = pw.ROOT


@pytest.mark.parametrize("value,text", [(4.914, "4.92"), (4.916, "4.92"), (4.911, "4.92"), (4.92, "4.92"), (4.9200000000000017, "4.92"),
                                        (4.921, "4.93"), (4.9, "4.90"), (0.0, "0.00"), (11.3325, "11.34"), (3.416, "3.42"), (10, "10.00")])
def test_the_hold_is_rounded_up_to_the_cent_and_a_float_that_is_a_cent_already_is_not_pushed_over(value, text):
    assert packet.cents_up(value) == text


def test_a_hold_of_4_914_is_typed_as_4_92_not_4_91(monkeypatch):
    monkeypatch.setattr(ask, "hold_usd", lambda tier, model: 4.914)
    assert packet.hold_ceiling() == "4.92"
    assert f"{4.914:.2f}" == "4.91", "the figure the old code typed"


def test_the_hold_of_this_tree_is_the_figure_the_line_has_always_had():
    assert packet.hold_ceiling() == "4.92" == packet.cents_up(ask.hold_usd("T1", ask.MODEL))


# the hold is read from the bound commit

def fake_repo(tmp_path, value):
    repo = tmp_path / "bound"
    (repo / "core/agent").mkdir(parents=True)
    (repo / "core/__init__.py").write_text("", encoding="utf-8")
    (repo / "core/agent/__init__.py").write_text("", encoding="utf-8")
    (repo / "core/agent/ask.py").write_text(f"MODEL = 'm'\n\n\ndef hold_usd(tier, model):\n    assert (tier, model) == ('T1', 'm')\n    return {value}\n",
                                            encoding="utf-8")
    pw.git(repo, "init", "-q")
    pw.git(repo, "add", "-A")
    pw.git(repo, "commit", "-q", "-m", "bound")
    return repo, pw.head(repo)


def test_the_hold_comes_from_the_bound_commit_not_from_the_checkout_that_runs_the_tool(tmp_path):
    repo, commit = fake_repo(tmp_path, 7.001)
    assert packet.hold_ceiling(repo, commit) == "7.01" != packet.hold_ceiling()
    assert "core.agent.ask" in sys.modules and sys.modules["core.agent.ask"].__file__.startswith(str(ROOT))


def test_a_change_to_the_working_tree_after_the_commit_is_not_the_hold(tmp_path):
    repo, commit = fake_repo(tmp_path, 7.001)
    (repo / "core/agent/ask.py").write_text("MODEL = 'm'\n\n\ndef hold_usd(tier, model):\n    return 1.0\n", encoding="utf-8")
    assert packet.hold_ceiling(repo, commit) == "7.01"


def test_the_running_checkouts_modules_are_not_the_ones_the_bound_commit_is_read_with(tmp_path):
    repo, commit = fake_repo(tmp_path, 3.0)
    (repo / "core/agent/other.py").write_text("raise SystemExit('should never be imported')\n", encoding="utf-8")
    assert packet.hold_ceiling(repo, commit) == "3.00"


@pytest.mark.parametrize("body", ["raise RuntimeError('no')", "def hold_usd(tier, model):\n    return 'many'", "def hold_usd(tier, model):\n    return float('nan')",
                                  "def hold_usd(tier, model):\n    return -1.0", "MODEL = 'm'"])
def test_a_bound_commit_that_gives_no_usable_hold_is_refused(tmp_path, body):
    repo, commit = fake_repo(tmp_path, 1.0)
    (repo / "core/agent/ask.py").write_text(("MODEL = 'm'\n" if "MODEL" not in body else "") + body + "\n", encoding="utf-8")
    pw.git(repo, "commit", "-q", "-am", "bad")
    with pytest.raises(packet.Refused):
        packet.hold_ceiling(repo, pw.head(repo))


def test_a_commit_the_repository_does_not_hold_is_refused(tmp_path):
    repo, _ = fake_repo(tmp_path, 1.0)
    with pytest.raises(packet.Refused):
        packet.hold_ceiling(repo, "0" * 40)


def test_the_real_bound_tree_gives_the_figure_of_the_code_in_it():
    assert packet.hold_ceiling(ROOT, pw.head(ROOT)) == packet.cents_up(ask.hold_usd("T1", ask.MODEL))


# the receipt command types the line against the hold of the bound repository

def run(argv, capsys):
    code = packet.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def reviewed(tmp_path, monkeypatch, capsys):
    bench = pw.Bench(tmp_path, monkeypatch)
    bench.prepare_inputs()
    for argv in (bench.capture_argv(), bench.bind_argv(), bench.lock_argv(), bench.review_argv()):
        code, out, err = run(argv, capsys)
        assert code == 0, err
    return bench


def test_the_receipt_asks_the_bound_repository_and_commit_for_the_hold(tmp_path, monkeypatch, capsys):
    bench = reviewed(tmp_path, monkeypatch, capsys)
    asked = []

    def bound_hold(repo, commit):
        asked.append((str(repo), commit))
        return 9.001

    monkeypatch.setattr(packet, "bound_hold", bound_hold)
    bench.line.write_text(pw.authorisation_line(bench.release_id) + "\n", encoding="utf-8")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "authorisation" in err and asked == [(str(bench.repo), bench.target)]
    bench.line.write_text(pw.authorisation_line(bench.release_id).replace("USD 4.92", "USD 9.01") + "\n", encoding="utf-8")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 0, err


def test_the_receipt_is_refused_when_the_checkout_is_not_at_the_bound_commit(tmp_path, monkeypatch, capsys):
    bench = reviewed(tmp_path, monkeypatch, capsys)
    (bench.repo / "core/setup/release/plan.py").write_text("moved\n", encoding="utf-8")
    pw.git(bench.repo, "commit", "-q", "-am", "moves head")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "bound commit" in err and not bench.receipt.exists()


def test_the_receipt_takes_the_line_of_the_real_hold_of_the_bound_tree(tmp_path, monkeypatch, capsys):
    bench = reviewed(tmp_path, monkeypatch, capsys)
    assert (bench.repo / "core/agent/ask.py").is_file(), "the throwaway repository must hold the closure of ask for the hold to be read from it"
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 0, err
