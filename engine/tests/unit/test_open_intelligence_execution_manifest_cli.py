import hashlib
import inspect
import json
from pathlib import Path

import pytest
from scripts.staging import build_open_intelligence_execution_manifest as builder
from src.analysis.open_intelligence import execution_approval

from tests.unit.test_operator_callers_v2 import generation, registry, retained_contract

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "execution_manifest_brain_read_v1.json"
)


@pytest.fixture(autouse=True)
def _packaged_generation(monkeypatch):
    # The retained fixture keeps a placeholder contract digest; the builder now selects
    # the retained v1 row by its real digest under explicit historical read.
    monkeypatch.setattr(builder, "_active_generation", generation)


def _fixture_payload() -> dict[str, object]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["contract_sha256"] = retained_contract("brain_read")
    return payload


def _canonical(payload) -> bytes:
    return execution_approval.canonical_manifest_bytes(
        payload, mode="historical_read", registry=registry()
    )


def _write_request(path: Path, payload: object | None = None) -> None:
    value = _fixture_payload() if payload is None else payload
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _expected_error(code: str) -> str:
    return json.dumps({"error": code}, separators=(",", ":"), sort_keys=True) + "\n"


def test_public_main_accepts_only_argv():
    signature = inspect.signature(builder.main)
    assert tuple(signature.parameters) == ("argv",)
    assert signature.parameters["argv"].default is None


def test_builder_writes_exact_canonical_bytes_and_one_receipt_line(tmp_path, capsys):
    request = tmp_path / "request.json"
    output = tmp_path / "manifest.json"
    payload = dict(reversed(tuple(_fixture_payload().items())))
    request.write_text(json.dumps(payload, indent=4), encoding="utf-8")

    assert (
        builder.main(
            ["--request-file", str(request.resolve()), "--output-file", str(output.resolve())]
        )
        == 0
    )

    expected = _canonical(_fixture_payload())
    assert output.read_bytes() == expected
    receipt = {
        "contract_version": "open_intelligence_execution_manifest_build_v1",
        "manifest_file": str(output.resolve()),
        "manifest_sha256": hashlib.sha256(expected).hexdigest(),
        "operation": "brain_read",
    }
    captured = capsys.readouterr()
    assert captured.out == json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n"
    assert captured.err == ""


@pytest.mark.parametrize(
    "argv_factory",
    [
        lambda request, output: [
            "--output-file",
            str(output),
            "--request-file",
            str(request),
        ],
        lambda request, output: [
            "--request",
            str(request),
            "--output-file",
            str(output),
        ],
        lambda request, output: [
            "--request-file",
            str(request),
            "--request-file",
            str(output),
        ],
        lambda _request, output: [
            "--request-file",
            "request.json",
            "--output-file",
            str(output),
        ],
        lambda request, _output: [
            "--request-file",
            str(request),
            "--output-file",
            "manifest.json",
        ],
        lambda request, output: [
            "--request-file",
            str(request),
            "--output-file",
            str(output),
            "--project",
            "ogilvy-trends-v2",
        ],
        lambda request, _output: ["--request-file", str(request)],
        lambda request, output: [str(request), str(output)],
    ],
)
def test_builder_refuses_every_nonexact_cli_grammar(argv_factory, tmp_path, capsys):
    request = tmp_path / "request.json"
    output = tmp_path / "manifest.json"
    _write_request(request)

    assert builder.main(argv_factory(request.resolve(), output.resolve())) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == _expected_error("execution_approval_cli_invalid")
    assert output.exists() is False


def test_builder_refuses_an_existing_output_without_changing_it(tmp_path, capsys):
    request = tmp_path / "request.json"
    output = tmp_path / "manifest.json"
    _write_request(request)
    output.write_bytes(b"existing")

    assert (
        builder.main(
            ["--request-file", str(request.resolve()), "--output-file", str(output.resolve())]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == _expected_error("execution_approval_output_exists")
    assert output.read_bytes() == b"existing"


@pytest.mark.parametrize(
    ("request_bytes", "error"),
    [
        (
            b'{"operation":"brain_read","operation":"brain_read"}',
            "execution_approval_manifest_invalid",
        ),
        (b"\xef\xbb\xbf{}", "execution_approval_manifest_invalid"),
        (b"\xff", "execution_approval_manifest_invalid"),
        (b"{} {}", "execution_approval_manifest_invalid"),
        (b'{"value":NaN}', "execution_approval_manifest_invalid"),
    ],
)
def test_builder_refuses_noncanonical_json_inputs(request_bytes, error, tmp_path, capsys):
    request = tmp_path / "request.json"
    output = tmp_path / "manifest.json"
    request.write_bytes(request_bytes)

    assert (
        builder.main(
            ["--request-file", str(request.resolve()), "--output-file", str(output.resolve())]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == _expected_error(error)
    assert output.exists() is False


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ({"extra": "forbidden"}, "execution_approval_manifest_invalid"),
        ({"project": "other-project"}, "execution_approval_target_invalid"),
    ],
)
def test_builder_refuses_schema_drift_and_alternate_targets(mutation, error, tmp_path, capsys):
    request = tmp_path / "request.json"
    output = tmp_path / "manifest.json"
    payload = _fixture_payload()
    payload.update(mutation)
    _write_request(request, payload)

    assert (
        builder.main(
            ["--request-file", str(request.resolve()), "--output-file", str(output.resolve())]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == _expected_error(error)
    assert output.exists() is False


def test_builder_removes_only_a_zero_byte_output_created_by_failed_write(
    tmp_path, capsys, monkeypatch
):
    request = tmp_path / "request.json"
    output = tmp_path / "manifest.json"
    _write_request(request)
    real_open = Path.open

    class FailingWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def write(self, _value):
            raise OSError("injected write failure")

        def __exit__(self, exc_type, exc, traceback):
            self.handle.close()

    def failing_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if path == output and mode == "xb":
            return FailingWriter(handle)
        return handle

    monkeypatch.setattr(Path, "open", failing_open)

    assert (
        builder.main(
            ["--request-file", str(request.resolve()), "--output-file", str(output.resolve())]
        )
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == _expected_error("execution_approval_manifest_write_failed")
    assert output.exists() is False
