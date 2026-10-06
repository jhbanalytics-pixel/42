from pathlib import Path

import pytest

from core.eval import ask_r2, ask_r3


def test_main_delegates_to_shared_runtime_with_r3_isolation(tmp_path, monkeypatch):
    calls = {}
    module_origins = []
    repo_root = tmp_path / "repo"
    gate_path = repo_root / "plans" / "r3-health-gate.json"
    output_path = tmp_path / "ASK-2026-09-30-R3.md"
    data_dir = tmp_path / "ASK-2026-09-30-R3-data"
    morning_dir = tmp_path / "ASK-2026-09-30-data"
    original_prefix = ask_r2.PREFIX_ROOT

    def fake_main(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        calls["prefix"] = ask_r2.PREFIX_ROOT
        return 17

    def fake_module_origin(root, module):
        module_origins.append(module)
        return str(Path(module.__file__).resolve())

    monkeypatch.setattr(ask_r2, "main", fake_main)
    monkeypatch.setattr(ask_r2, "_module_origin", fake_module_origin)

    result = ask_r3.main(
        ["--execute"],
        repo_root=repo_root,
        gate_path=gate_path,
        output_path=output_path,
        data_dir=data_dir,
        morning_dir=morning_dir,
    )

    assert result == 17
    assert ask_r3.PREFIX_ROOT == "l3-ask-20260930-r3"
    assert ask_r3.OUTPUT_PATH.name == "ASK-2026-09-30-R3.md"
    assert ask_r3.DATA_DIR.name == "ASK-2026-09-30-R3-data"
    assert ask_r2.OUTPUT_PATH.name == "ASK-2026-09-30-R2.md"
    assert ask_r2.DATA_DIR.name == "ASK-2026-09-30-R2-data"
    assert calls["argv"] == ["--execute"]
    assert calls["prefix"] == "l3-ask-20260930-r3"
    assert calls["kwargs"] == {
        "repo_root": repo_root,
        "runner_path": Path(ask_r3.__file__).resolve(),
        "gate_path": gate_path,
        "output_path": output_path,
        "data_dir": data_dir,
        "morning_dir": morning_dir,
        "evaluation_label": "R3",
        "provider_500_retries": 1,
        "allow_resume": False,
        "r3_anchor_report_path": ask_r3.INITIAL_REPORT_PATH,
    }
    assert set(module_origins) == {ask_r2, ask_r3}
    assert ask_r2.PREFIX_ROOT == original_prefix


def test_resume_delegates_to_shared_runtime_with_r3_continuation_authority(tmp_path, monkeypatch):
    calls = {}

    def fake_main(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        calls["prefix"] = ask_r2.PREFIX_ROOT
        return 17

    monkeypatch.setattr(ask_r2, "main", fake_main)
    monkeypatch.setattr(ask_r2, "_module_origin", lambda root, module: str(Path(module.__file__)))

    result = ask_r3.main(["--execute", "--resume"], repo_root=tmp_path)

    assert result == 17
    assert calls["argv"] == ["--execute", "--resume"]
    assert calls["prefix"] == ask_r3.PREFIX_ROOT
    assert calls["kwargs"]["evaluation_label"] == "R3"
    assert calls["kwargs"]["allow_resume"] is True
    assert calls["kwargs"]["r3_anchor_report_path"] == ask_r3.INITIAL_REPORT_PATH


def test_prefix_is_restored_when_shared_runtime_raises(monkeypatch):
    original_prefix = ask_r2.PREFIX_ROOT

    def fake_main(*args, **kwargs):
        assert ask_r2.PREFIX_ROOT == "l3-ask-20260930-r3"
        raise RuntimeError("operator failure")

    monkeypatch.setattr(ask_r2, "main", fake_main)
    monkeypatch.setattr(ask_r2, "_module_origin", lambda root, module: str(module.__file__))

    with pytest.raises(RuntimeError, match="operator failure"):
        ask_r3.main(["--execute"])

    assert ask_r2.PREFIX_ROOT == original_prefix
