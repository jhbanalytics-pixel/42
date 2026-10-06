import hashlib
import importlib
import json
from pathlib import Path

import pytest


def module():
    return importlib.import_module("scripts.build_general_question_context")


def write(root, name, data=b"pass\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def inputs(tmp_path, monkeypatch):
    engine, lp = tmp_path / "engine_source", tmp_path / "lp_source"
    for name in (
        "src/entry.py",
        "scripts/staging/run_general_question_worker.py",
        "configs/rules.yaml",
        "infra/bigquery_schemas/example.sql",
        "requirements.lock",
    ):
        write(engine, name)
    for name in (
        "src/api/main.py",
        "configs/scope.yaml",
        "requirements.txt",
        "web/dist/index.html",
    ):
        write(lp, name)
    write(
        lp,
        "Dockerfile.general-question",
        (
            Path(__file__).resolve().parents[2] / "Dockerfile.general-question"
        ).read_bytes(),
    )
    monkeypatch.setattr(
        module(),
        "_git_state",
        lambda root, *, kind: {"commit": "a" * 40, "dirty": False},
    )
    review = {
        "contract_version": "general_question_context_review_v1",
        "allow_reviewed_working_tree": False,
        "engine": module().inventory_source(engine, kind="engine"),
        "lp": module().inventory_source(lp, kind="lp"),
    }
    return engine, lp, review


def test_context_is_deterministic_and_verified_by_existing_bundle_reader(
    tmp_path, monkeypatch
):
    engine, lp, review = inputs(tmp_path, monkeypatch)
    first = module().build_context(
        engine_root=engine,
        lp_root=lp,
        destination=tmp_path / "one",
        review_manifest=review,
    )
    second = module().build_context(
        engine_root=engine,
        lp_root=lp,
        destination=tmp_path / "two",
        review_manifest=review,
    )
    assert first == second
    manifest = json.loads((tmp_path / "one/engine/bundle-manifest.json").read_bytes())
    assert "requirements.lock" in manifest["files"]
    assert (
        first["engine_bundle_digest"]
        == hashlib.sha256(module().canonical(manifest)).hexdigest()
    )
    assert first["deployment_ready"] is True
    assert (tmp_path / "one/runtime-build.json").read_bytes() == (
        tmp_path / "two/runtime-build.json"
    ).read_bytes()


@pytest.mark.parametrize(
    "mutation",
    [
        "tamper",
        "extra",
        "missing",
        "dirty",
        "conflict",
        "traversal",
        "bad_review",
        "credential",
        "symlink",
    ],
)
def test_unreviewed_or_unsafe_sources_refuse_before_output(
    tmp_path, monkeypatch, mutation
):
    engine, lp, review = inputs(tmp_path, monkeypatch)
    if mutation == "tamper":
        write(engine, "src/entry.py", b"changed")
    elif mutation == "extra":
        write(engine, "src/extra.py")
    elif mutation == "missing":
        (lp / "web/dist/index.html").unlink()
    elif mutation == "dirty":
        monkeypatch.setattr(
            module(),
            "_git_state",
            lambda root, *, kind: {"commit": "a" * 40, "dirty": True},
        )
    elif mutation == "conflict":

        def conflict(_root, *, kind):
            raise ValueError("source_conflict")

        monkeypatch.setattr(module(), "_git_state", conflict)
    elif mutation == "traversal":
        review["engine"]["files"]["../escape.py"] = "a" * 64
    elif mutation == "bad_review":
        review["allow_reviewed_working_tree"] = "yes"
    elif mutation == "credential":
        write(
            engine,
            "configs/runtime.json",
            b'{"type":"service_account","private_key":"synthetic"}',
        )
    else:
        link = engine / "src/linked.py"
        try:
            link.symlink_to(engine / "src/entry.py")
        except OSError:
            pytest.skip("host does not permit symlink fixture")
    with pytest.raises(ValueError):
        module().build_context(
            engine_root=engine,
            lp_root=lp,
            destination=tmp_path / "out",
            review_manifest=review,
        )
    assert not (tmp_path / "out").exists()


def test_reviewed_working_tree_is_bound_but_not_labelled_clean_deployment(
    tmp_path, monkeypatch
):
    engine, lp, review = inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(
        module(),
        "_git_state",
        lambda root, *, kind: {"commit": "a" * 40, "dirty": True},
    )
    review["engine"]["dirty"] = review["lp"]["dirty"] = True
    review["allow_reviewed_working_tree"] = True
    receipt = module().build_context(
        engine_root=engine,
        lp_root=lp,
        destination=tmp_path / "out",
        review_manifest=review,
    )
    assert receipt["deployment_ready"] is False
    assert receipt["commit_role"] == "base_revision_with_reviewed_working_bytes"


def test_tests_metadata_and_bytecode_are_excluded_without_reading_them(
    tmp_path, monkeypatch
):
    engine, lp, review = inputs(tmp_path, monkeypatch)
    for name in (
        "tests/test_example.py",
        "src/__pycache__/example.pyc",
        "src/.env",
        "NOTES.md",
    ):
        write(engine, name)
    receipt = module().build_context(
        engine_root=engine,
        lp_root=lp,
        destination=tmp_path / "out",
        review_manifest=review,
    )
    assert receipt["engine"]["files"] == review["engine"]["files"]
    assert not (tmp_path / "out/engine/tests").exists()


def test_docker_keeps_parent_and_worker_dependency_environments_separate():
    docker = (
        Path(__file__).resolve().parents[2] / "Dockerfile.general-question"
    ).read_text()
    assert "python -m venv --copies /opt/42-engine-venv" in docker
    assert "/opt/42-engine-venv/bin/python -m pip install --require-hashes" in docker
    assert "/app/engine/requirements.lock" in docker
    assert "PYTHONDONTWRITEBYTECODE=1" in docker
    assert "COPY engine/ /app/engine/" in docker
    assert "COPY lp/src/ /app/src/" in docker
    assert "COPY lp/requirements.lock /tmp/lp-requirements.lock" in docker
