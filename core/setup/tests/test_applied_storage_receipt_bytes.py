"""The applied-storage receipt's byte contract on Windows: S3 records the hash of the bytes S4 later reads.

Beta 2.4.5 stopped at S4 on 16 Oct 2026 with "Locked applied-storage receipt differs": on Windows the receipt was written in text
mode with CRLF line endings, durable_prior recorded the hash of read_text (LF), and receipt_path hashed the stored bytes. Here Windows
text mode is reproduced on any platform: a text-mode Path.open with no newline argument writes CRLF, as it does on Windows.
"""
import hashlib
import json
from pathlib import Path

import pytest

from core.setup.release import applied_storage as storage
from core.setup.release import durable_prior

ROOT = Path(__file__).resolve().parents[3]
F9 = "f9bd07c9d4d70b140dc13811a5da745d5889b885"
BODY = {"schema_version": 1, "kind": storage.KIND, "jobs": {"release_id": "rel-0000000-01", "packet_dir": "C:\\jobs"},
        "captured_utc": "2026-10-16T14:30:00+00:00", "deferred_effect_ids": ["E-JOB-VIEWS"]}


@pytest.fixture
def windows_text_mode(monkeypatch):
    real_open = Path.open

    def open_as_on_windows(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        # Windows text mode writes "\n" as CRLF; reading with newline=None turns CRLF back into "\n" on every platform
        if "b" not in mode and newline is None and any(c in mode for c in "wxa+"):
            newline = "\r\n"
        return real_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", open_as_on_windows)


def test_the_simulation_writes_crlf_as_windows_does(windows_text_mode, tmp_path):
    path = tmp_path / "plain.txt"
    with path.open("x", encoding="utf-8") as out:
        out.write("a\nb\n")
    assert path.read_bytes() == b"a\r\nb\r\n" and path.read_text(encoding="utf-8") == "a\nb\n"


def test_the_receipt_is_written_with_lf_and_hashed_over_its_bytes(windows_text_mode, tmp_path):
    path = tmp_path / storage.RECEIPT_FILE
    storage.write_receipt(path, BODY)
    raw = path.read_bytes()
    assert b"\r" not in raw and json.loads(raw) == BODY
    assert storage.receipt_sha256(path) == hashlib.sha256(raw).hexdigest()
    effect = storage.receipt_effect("rel-0000000-01", storage.receipt_sha256(path), {"lane": 3, "person": "Fixture"}, 1)
    assert storage.receipt_path({"effects": [effect]}, tmp_path) == path


def test_s4_still_refuses_a_receipt_whose_bytes_differ_from_the_recorded_hash(tmp_path):
    path = tmp_path / storage.RECEIPT_FILE
    text = json.dumps(BODY, indent=2, sort_keys=True) + "\n"
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    effect = storage.receipt_effect("rel-0000000-01", text_hash, {"lane": 3, "person": "Fixture"}, 1)
    with pytest.raises(storage.Refused, match="Locked applied-storage receipt differs"):
        storage.receipt_path({"effects": [effect]}, tmp_path)


@pytest.mark.parametrize("line_end", [b"\n", b"\r\n"])
def test_s3_records_the_bytes_s4_reads_from_producer_to_consumer(windows_text_mode, monkeypatch, tmp_path, line_end):
    """durable_prior.build_manifest with the composed storage path; produce stands in for the native reads and leaves either line ending,
    so the recorded hash is proved to come from the stored bytes whatever the writer did."""
    output = tmp_path / "services" / storage.RECEIPT_FILE
    output.parent.mkdir()

    def produce(repo, head, tree, packet, out, native_factory=None):
        Path(out).write_bytes((json.dumps(BODY, indent=2, sort_keys=True) + "\n").encode("utf-8").replace(b"\n", line_end))
        return BODY

    monkeypatch.setattr(storage, "produce", produce)
    monkeypatch.setattr(durable_prior, "read_receipt", lambda *args: "checked old-reader receipt")
    monkeypatch.setattr(durable_prior, "stored_effect", lambda *args: None)
    manifest = durable_prior.build_manifest(ROOT, durable_prior.git(ROOT, "rev-parse", "HEAD").strip(), F9, "Fixture owner",
                                            old_reader_receipt=tmp_path / "old-reader-receipt.json", applied_jobs_packet=tmp_path / "jobs",
                                            storage_output=output)
    effect = next(e for e in manifest["effects"] if e["effect_id"] == storage.EFFECT_ID)
    stored = hashlib.sha256(output.read_bytes()).hexdigest()
    assert effect["evidence"]["applied_jobs_storage"]["receipt_sha256"] == stored
    assert effect["native_readback"]["expected_result_sha256"] == stored
    assert storage.receipt_path(manifest, output.parent) == output
