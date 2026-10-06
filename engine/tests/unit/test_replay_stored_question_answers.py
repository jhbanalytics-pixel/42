import hashlib
import importlib.util
import json
import socket
from pathlib import Path

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes

from tests.unit import test_general_question_runtime as runtime_fixture
from tests.unit import test_general_question_store as store_fixture

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/staging/replay_stored_question_answers.py"


def tool():
    spec = importlib.util.spec_from_file_location("replay_stored_question_answers", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lenient(value, **_):
    return value


class FakeBlob:
    def __init__(self, name, generation, raw, deleted=False):
        self.name, self.generation, self.size = name, generation, len(raw)
        self.time_deleted = "2026-09-20T00:00:00Z" if deleted else None
        self._raw = raw

    def download_as_bytes(self, *, if_generation_match, raw_download):
        assert raw_download is True
        assert if_generation_match == self.generation
        return self._raw


class FakeClient:
    def __init__(self, bucket):
        self.bucket = bucket
        self.calls = []

    def list_blobs(self, bucket_name, *, prefix, versions):
        self.calls.append((bucket_name, prefix, versions))
        for (name, generation), raw in sorted(self.bucket.versions.items()):
            if name.startswith(prefix):
                live = self.bucket.objects.get(name, (None,))[0] == generation
                yield FakeBlob(name, generation, raw, deleted=not live)


def mirror(bucket, root):
    return tool().capture(FakeClient(bucket), root)


def rewrite(root, key, change):
    """Change one live object in a mirror and reseal its manifest entry."""
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    name = store_fixture.PREFIX + key
    entry = next(item for item in manifest["objects"] if item["name"] == name and item["live"])
    path = root / "objects" / tool()._object_file(name, entry["generation"])
    raw = change(path.read_bytes())
    path.write_bytes(raw)
    entry.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


async def completed(monkeypatch, *, source_window=False):
    """A stored request answered and published the way the worker publishes it."""
    values = await runtime_fixture.answering_fixture(monkeypatch, source_window=source_window)
    runtime_fixture.intercept(monkeypatch, values[5])
    result = await runtime_fixture.answer(values)
    store = values[0]

    def validate_answer(response, *, request, plan, snapshot):
        return runtime_fixture.runtime_module().validate_persisted_question_answer(
            response,
            store=store,
            scope=store_fixture.scope(),
            request=request,
            plan=plan,
            snapshot=snapshot,
            validate_snapshot=lenient,
        )

    store.publish_result(
        values[2]["request_id"],
        scope=store_fixture.scope(),
        now=store_fixture.NOW,
        answer_validator=validate_answer,
        **result,
    )
    return values, result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_window, planning, answering",
    [(False, "continuity_v2", "typed_v4"), (True, "discovery_challenge_v6", "typed_v5")],
)
async def test_stored_answer_replays_under_its_recorded_adapters_without_network_or_writes(
    monkeypatch, tmp_path, source_window, planning, answering
):
    values, result = await completed(monkeypatch, source_window=source_window)
    bucket, invocation = values[1], values[2]
    root = tmp_path / "mirror"
    manifest = mirror(bucket, root)
    assert manifest["objects"]
    monkeypatch.setattr(socket.socket, "connect", lambda *_: pytest.fail("network during replay"))
    report = tool().replay(root, snapshot_validator=lenient)
    assert report["write_attempts"] == []
    assert report["outcomes"] == {"replayed": 1}
    (row,) = report["requests"]
    assert row["request_id"] == invocation["request_id"]
    assert row["versions"]["planning"]["adapter"] == planning
    assert row["versions"]["answering"]["adapter"] == answering
    assert row["checks"] == {
        "planning_request_rebuilt": True,
        "published_result_reprojected": True,
    }
    assert row["replay_class"] == "answer_reprojected"
    assert report["replay_classes"] == {"answer_reprojected": 1}
    assert report["mirror_integrity_failures"] == []
    assert row["result_status"] == result["response"]["intelligence"]["status"]
    assert report["adapters"] == {"answering:" + answering: 1, "planning:" + planning: 1}


@pytest.mark.asyncio
async def test_changed_raw_answer_refuses_replay(monkeypatch, tmp_path):
    values, _ = await completed(monkeypatch)
    root = tmp_path / "mirror"
    mirror(values[1], root)
    key = f"requests/{values[2]['request_id']}/calls/answering.json"
    rewrite(root, key, lambda raw: raw.replace(b"Shared", b"Shored", 1))
    (row,) = tool().replay(root, snapshot_validator=lenient)["requests"]
    assert row["outcome"] == "refused"
    assert "published_result_reprojected" not in row["checks"]


@pytest.mark.asyncio
async def test_a_snapshot_the_validator_refuses_stops_the_replay(monkeypatch, tmp_path):
    values, _ = await completed(monkeypatch)
    root = tmp_path / "mirror"
    mirror(values[1], root)
    tool_module = tool()
    report = tool_module.replay(root, snapshot_validator=lenient)
    assert report["outcomes"] == {"replayed": 1}

    def strict(value, **_):
        raise ValueError("snapshot changed")

    (row,) = tool_module.replay(root, snapshot_validator=strict)["requests"]
    assert row["outcome"] == "refused"
    assert row["error"] == "snapshot_invalid"


@pytest.mark.asyncio
async def test_default_replay_uses_the_stored_snapshot_validator(monkeypatch, tmp_path):
    values, _ = await completed(monkeypatch)
    root = tmp_path / "mirror"
    mirror(values[1], root)
    seen = []
    from src.analysis.open_intelligence import general_question_execution

    def recorded(snapshot, **kwargs):
        seen.append(kwargs["request"]["request_id"])
        return snapshot

    monkeypatch.setattr(
        general_question_execution, "validate_stored_general_question_snapshot", recorded
    )
    report = tool().replay(root)
    assert report["outcomes"] == {"replayed": 1}
    assert seen == [values[2]["request_id"]]


def rebind(root, request_id, stage, field, value):
    """Change one recorded binding field consistently in the call object and the ledger."""
    digest = {}

    def call_object(raw):
        stored = json.loads(raw)
        stored["binding"][field] = value
        changed = canonical_bytes(stored)
        digest["value"] = hashlib.sha256(changed).hexdigest()
        return changed

    rewrite(root, f"requests/{request_id}/calls/{stage}.json", call_object)

    def ledger(raw):
        control = json.loads(raw)
        call = control["requests"][request_id]["execution"]["calls"][stage]
        call[field] = value
        call["response"]["digest"] = digest["value"]
        return canonical_bytes(control)

    rewrite(root, store_fixture.LEDGER, ledger)


@pytest.mark.asyncio
async def test_ledger_binding_that_disagrees_with_the_stored_call_refuses(monkeypatch, tmp_path):
    values, _ = await completed(monkeypatch)
    root = tmp_path / "mirror"
    mirror(values[1], root)
    request_id = values[2]["request_id"]

    def cross(raw):
        control = json.loads(raw)
        call = control["requests"][request_id]["execution"]["calls"]["answering"]
        call["response_schema_digest"] = "0" * 64
        return canonical_bytes(control)

    rewrite(root, store_fixture.LEDGER, cross)
    (row,) = tool().replay(root, snapshot_validator=lenient)["requests"]
    assert (row["outcome"], row["error"]) == ("refused", "response_invalid")
    assert "versions" not in row


@pytest.mark.asyncio
async def test_recorded_answer_digests_outside_the_catalogue_refuse_version_resolution(
    monkeypatch, tmp_path
):
    values, _ = await completed(monkeypatch)
    root = tmp_path / "mirror"
    mirror(values[1], root)
    rebind(root, values[2]["request_id"], "answering", "response_schema_digest", "0" * 64)
    (row,) = tool().replay(root, snapshot_validator=lenient)["requests"]
    assert (row["outcome"], row["error"]) == (
        "refused",
        "ValueError: response_version_unresolved",
    )
    assert row["checks"] == {"planning_request_rebuilt": True}
    assert "versions" not in row


@pytest.mark.asyncio
async def test_planning_call_that_its_old_adapter_cannot_rebuild_refuses(monkeypatch, tmp_path):
    values, _ = await completed(monkeypatch)
    root = tmp_path / "mirror"
    mirror(values[1], root)
    rebind(root, values[2]["request_id"], "planning", "input_digest", "0" * 64)
    (row,) = tool().replay(root, snapshot_validator=lenient)["requests"]
    assert (row["outcome"], row["error"]) == ("refused", "ValueError: planning binding is invalid")
    assert row["checks"] == {}


@pytest.mark.asyncio
async def test_mirror_object_that_disagrees_with_its_manifest_is_refused(monkeypatch, tmp_path):
    values, _ = await completed(monkeypatch)
    root = tmp_path / "mirror"
    manifest = mirror(values[1], root)
    entry = next(item for item in manifest["objects"] if item["name"].endswith("plan.json"))
    path = root / "objects" / tool()._object_file(entry["name"], entry["generation"])
    path.write_bytes(path.read_bytes() + b" ")
    report = tool().replay(root, snapshot_validator=lenient)
    (row,) = report["requests"]
    assert (row["outcome"], row["error"]) == ("refused", "storage_unavailable")
    assert report["mirror_integrity_failures"] == [
        {"name": entry["name"], "generation": entry["generation"]}
    ]


@pytest.mark.parametrize(
    "row, expected",
    [
        (
            {
                "checks": {"published_result_reprojected": True},
                "calls": ["answering"],
                "result": True,
            },
            "answer_reprojected",
        ),
        (
            {
                "checks": {"published_result_reprojected": False},
                "calls": ["answering", "planning"],
                "result": True,
            },
            "answer_call_without_reprojected_result",
        ),
        (
            {"checks": {}, "calls": ["answering"], "result": False},
            "answer_call_without_reprojected_result",
        ),
        ({"checks": {}, "calls": ["planning"], "result": True}, "result_without_answer_call"),
        ({"checks": {}, "calls": [], "result": False}, "no_result"),
    ],
)
def test_replay_class_only_credits_an_actual_reprojection(row, expected):
    assert tool()._replay_class(row) == expected


def test_capture_keeps_every_version_and_refuses_an_existing_destination(tmp_path):
    bucket = store_fixture.Bucket()
    bucket.seed("policies/" + "a" * 64 + "/policy.json", {"value": 1})
    bucket.seed("policies/" + "a" * 64 + "/policy.json", {"value": 2})
    bucket.versions[("other/prefix.json", 99)] = b"{}"
    client = FakeClient(bucket)
    manifest = tool().capture(client, tmp_path / "mirror")
    assert client.calls == [(tool().BUCKET, tool().PREFIX, True)]
    assert [(item["generation"], item["live"]) for item in manifest["objects"]] == [
        (1, False),
        (2, True),
    ]
    for item in manifest["objects"]:
        raw = (
            tmp_path / "mirror/objects" / tool()._object_file(item["name"], item["generation"])
        ).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
    with pytest.raises(FileExistsError):
        tool().capture(client, tmp_path / "mirror")


def test_mirror_bucket_refuses_every_write(tmp_path):
    bucket = store_fixture.Bucket()
    bucket.seed("policies/" + "a" * 64 + "/policy.json", {"value": 1})
    tool().capture(FakeClient(bucket), tmp_path / "mirror")
    module = tool()
    mirrored = module.MirrorBucket(tmp_path / "mirror")
    with pytest.raises(module.ReplayWriteRefused):
        mirrored.blob(store_fixture.PREFIX + "x.json").upload_from_string(
            b"{}", if_generation_match=0, content_type="application/json"
        )
    assert mirrored.write_attempts == [store_fixture.PREFIX + "x.json"]


@pytest.mark.asyncio
async def test_unavailable_result_after_an_answer_call_is_not_credited_as_reprojected(
    monkeypatch, tmp_path
):
    from src.analysis.open_intelligence.general_question_result import (
        observed_result_usage,
        unavailable_response,
    )

    values = await runtime_fixture.answering_fixture(monkeypatch)
    runtime_fixture.intercept(monkeypatch, values[5])
    await runtime_fixture.answer(values)
    store, request_id = values[0], values[2]["request_id"]
    context = store.read_request(request_id, scope=store_fixture.scope())
    usage = observed_result_usage(store, request_id=request_id, scope=store_fixture.scope())
    store.publish_result(
        request_id,
        scope=store_fixture.scope(),
        response=unavailable_response(
            context["request"], usage, reason="request_expired", window=None, markets=None
        ),
        state="unavailable",
        now=store_fixture.NOW,
    )
    root = tmp_path / "mirror"
    mirror(values[1], root)
    report = tool().replay(root, snapshot_validator=lenient)
    (row,) = report["requests"]
    assert row["outcome"] == "replayed"
    assert row["checks"]["published_result_reprojected"] is False
    assert row["replay_class"] == "answer_call_without_reprojected_result"
    assert report["replay_classes"] == {"answer_call_without_reprojected_result": 1}


@pytest.mark.asyncio
async def test_support_review_packet_carries_exact_spans_and_blank_assessments(
    monkeypatch, tmp_path
):
    values, result = await completed(monkeypatch, source_window=True)
    root = tmp_path / "mirror"
    mirror(values[1], root)
    module = tool()
    packet = module.support_review_packet(root, tmp_path / "packet", snapshot_validator=lenient)
    (answer,) = packet["answers"]
    assert answer["request_id"] == values[2]["request_id"]
    assert answer["answer"] == result["response"]["answer"]
    claims = {claim["claim_id"]: claim for claim in answer["claims"]}
    assert set(claims) == {
        claim["claim_id"] for claim in result["response"]["intelligence"]["claims"]
    }
    (cited,) = claims["observed"]["cited"]
    (span,) = cited["quote_spans"]
    assert span["quote"] == cited["excerpt"][span["start"] : span["end"]]
    assert hashlib.sha256(span["quote"].encode()).hexdigest() == span["quote_sha256"]
    assert cited["evidence_purposes"] == ["support"]
    assert answer["market_scope"] == ["za"]
    assert packet["left_out"] == {}
    assert all(set(claim["assessment"].values()) == {None} for claim in answer["claims"])
    raw = (tmp_path / "packet/packet.json").read_bytes()
    assert raw == canonical_bytes(packet)
    text = (tmp_path / "packet/packet.md").read_text(encoding="utf-8")
    assert hashlib.sha256(raw).hexdigest() in text
    assert "Quoted span: " + span["quote"] in text
    assert "Markets za; window 2026-08-23 to 2026-09-05." in text
    assert "Requests left out, by reason: none." in text
    with pytest.raises(FileExistsError):
        module.support_review_packet(root, tmp_path / "packet", snapshot_validator=lenient)


@pytest.mark.asyncio
async def test_support_review_packet_leaves_out_answers_it_could_not_reproject(
    monkeypatch, tmp_path
):
    values, _ = await completed(monkeypatch)
    root = tmp_path / "mirror"
    mirror(values[1], root)
    key = f"requests/{values[2]['request_id']}/calls/answering.json"
    rewrite(root, key, lambda raw: raw.replace(b"Shared", b"Shored", 1))
    packet = tool().support_review_packet(root, tmp_path / "packet", snapshot_validator=lenient)
    assert packet["answers"] == []
    assert packet["left_out"] == {"not_reprojected": 1}


@pytest.mark.asyncio
async def test_support_review_packet_counts_but_leaves_out_an_unavailable_answer(
    monkeypatch, tmp_path
):
    from src.analysis.open_intelligence.general_question_result import (
        observed_result_usage,
        unavailable_response,
    )

    values = await runtime_fixture.answering_fixture(monkeypatch)
    runtime_fixture.intercept(monkeypatch, values[5])
    await runtime_fixture.answer(values)
    store, request_id = values[0], values[2]["request_id"]
    context = store.read_request(request_id, scope=store_fixture.scope())
    usage = observed_result_usage(store, request_id=request_id, scope=store_fixture.scope())
    store.publish_result(
        request_id,
        scope=store_fixture.scope(),
        response=unavailable_response(
            context["request"], usage, reason="request_expired", window=None, markets=None
        ),
        state="unavailable",
        now=store_fixture.NOW,
    )
    root = tmp_path / "mirror"
    mirror(values[1], root)
    packet = tool().support_review_packet(root, tmp_path / "packet", snapshot_validator=lenient)
    assert packet["answers"] == []
    assert packet["left_out"] == {"not_reprojected": 1}


def windows_text_writes(monkeypatch):
    """Write text the way Windows does: a text mode write left at the platform newline turns
    every \\n into \\r\\n. A writer that names its newline, or writes bytes, is unaffected."""
    import builtins
    import pathlib

    path_open, builtin_open = pathlib.Path.open, builtins.open

    def translated(mode, newline):
        if newline is None and "b" not in mode and set(mode) & set("wxa+"):
            return "\r\n"
        return newline

    def open_path(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        return path_open(self, mode, buffering, encoding, errors, translated(mode, newline))

    def open_builtin(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, *a):
        return builtin_open(file, mode, buffering, encoding, errors, translated(mode, newline), *a)

    monkeypatch.setattr(pathlib.Path, "open", open_path)
    monkeypatch.setattr(builtins, "open", open_builtin)


def test_windows_text_writes_simulation_translates_an_unnamed_newline(monkeypatch, tmp_path):
    windows_text_writes(monkeypatch)
    (tmp_path / "platform.txt").write_text("a\nb\n", encoding="utf-8")
    (tmp_path / "named.txt").write_text("a\nb\n", encoding="utf-8", newline="\n")
    assert (tmp_path / "platform.txt").read_bytes() == b"a\r\nb\r\n"
    assert (tmp_path / "named.txt").read_bytes() == b"a\nb\n"


def test_capture_writes_the_same_manifest_bytes_on_windows(monkeypatch, tmp_path):
    bucket = store_fixture.Bucket()
    bucket.seed("policies/" + "a" * 64 + "/policy.json", {"value": 1})
    windows_text_writes(monkeypatch)
    manifest = tool().capture(FakeClient(bucket), tmp_path / "mirror")
    raw = (tmp_path / "mirror/manifest.json").read_bytes()
    assert b"\r" not in raw
    assert raw == json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")


@pytest.mark.asyncio
async def test_support_review_packet_writes_the_same_bytes_on_windows(monkeypatch, tmp_path):
    values, _ = await completed(monkeypatch, source_window=True)
    root = tmp_path / "mirror"
    windows_text_writes(monkeypatch)
    manifest = mirror(values[1], root)
    packet = tool().support_review_packet(root, tmp_path / "packet", snapshot_validator=lenient)
    expected = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    assert packet["manifest_sha256"] == hashlib.sha256(expected).hexdigest()
    markdown = (tmp_path / "packet/packet.md").read_bytes()
    assert b"\n" in markdown
    assert b"\r" not in markdown
