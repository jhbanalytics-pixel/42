import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from src.api.question_worker_result import ResultVerificationError, decode_result_record


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EXPECTED = {
    "contract_version": "general_cultural_question_v1",
    "request_id": "00000000-0000-0000-0000-000000000001",
    "request_digest": "8c5169f79ab7d0d47cff99a021e293e21fa7e87e128897584e8193ab4b613d72",
    "intake_digest": "e701d979c7f74ca981f1663f613b8ac531fb63cc0e7095ba844f344de2ca5f86",
    "policy_digest": "d1128b377e0addde7f0fe720284d530e4d19df67e73a33e4e57e3ed9710e2b30",
    "deployment_digest": "d" * 64,
}
HASHES = {
    "unavailable": "12f88cc6df5957af047efeb333fb07bacd186b4d8307aa8163378d364fe1629e",
    "held": "778798015a7c992e1116d8c0993b800bb483dc951f36581950dcaa2a874e0edc",
}


def raw_fixture(state="unavailable"):
    return (FIXTURES / f"general-question-result-{state}.json").read_bytes()


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def receipt(state="unavailable", digest=None):
    return {
        key: EXPECTED[key] for key in ["request_id", "request_digest", "intake_digest"]
    } | {
        "result_digest": digest or HASHES[state],
        "result_generation": "17",
        "state": "held" if state == "held" else "terminal",
    }


def decode(data, *, state="unavailable", pointer=None, generation="17"):
    return decode_result_record(
        data,
        generation=generation,
        invocation=EXPECTED,
        receipt=pointer or receipt(state),
        max_bytes=8 * 1024 * 1024,
    )


@pytest.mark.parametrize("state", ["unavailable", "held"])
def test_real_offline_producer_bytes_preserve_public_unknown_usage(state):
    data = raw_fixture(state)
    assert hashlib.sha256(data).hexdigest() == HASHES[state]
    result = decode(data, state=state)
    assert result["response"]["intelligence"]["status"] == "unavailable"
    assert result["response"]["intelligence"]["ready_for_downstream"] is False
    assert result["response"]["intelligence"]["usage"]["model_calls"] == (
        None if state == "held" else 0
    )
    assert canonical(result) == data


@pytest.mark.parametrize("generation", ["18", 17, "0", None])
def test_read_generation_must_match_the_engine_pointer(generation):
    with pytest.raises(ResultVerificationError):
        decode(raw_fixture(), generation=generation)


def test_changed_object_bytes_cannot_use_the_original_pointer():
    with pytest.raises(ResultVerificationError):
        decode(raw_fixture().replace(b"expired", b"resolved"))


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "00000000-0000-0000-0000-000000000002"},
        {"request_digest": "f" * 64},
        {"intake_digest": "f" * 64},
        {"policy_digest": "f" * 64},
        {"deployment_digest": "f" * 64},
        {"state": "complete"},
        {"contract_version": "other"},
        {"recorded_at": "2026-09-07T00:00:00"},
        {"extra": True},
        {"response": {}},
        {"plan_digest": "bad"},
        {"snapshot_digest": "bad"},
    ],
)
def test_pinned_bytes_still_require_original_bindings_and_valid_envelope(change):
    data = canonical(json.loads(raw_fixture()) | change)
    with pytest.raises(ResultVerificationError):
        decode(data, pointer=receipt(digest=hashlib.sha256(data).hexdigest()))


@pytest.mark.parametrize(
    "transform",
    [
        lambda data: data + b"\n",
        lambda data: json.dumps(json.loads(data), indent=2).encode(),
        lambda data: b'{"state":"ignored",' + data[1:],
    ],
)
def test_even_a_matching_digest_cannot_authorize_noncanonical_or_duplicate_json(
    transform,
):
    data = transform(raw_fixture())
    with pytest.raises(ResultVerificationError):
        decode(data, pointer=receipt(digest=hashlib.sha256(data).hexdigest()))


@pytest.mark.parametrize(
    "change",
    [
        {"request_id": "00000000-0000-0000-0000-000000000002"},
        {"request_digest": "f" * 64},
        {"status": "complete"},
        {"ready_for_downstream": True},
    ],
)
def test_held_record_cannot_project_another_request_or_readiness(change):
    value = json.loads(raw_fixture("held"))
    value["response"]["intelligence"].update(change)
    data = canonical(value)
    with pytest.raises(ResultVerificationError):
        decode(
            data,
            state="held",
            pointer=receipt("held", hashlib.sha256(data).hexdigest()),
        )


def test_held_receipt_cannot_acknowledge_an_unavailable_record():
    with pytest.raises(ResultVerificationError):
        decode(raw_fixture(), pointer=receipt("held", HASHES["unavailable"]))


def test_held_partial_response_requires_plan_and_snapshot_bindings():
    script = """
import {readFileSync} from 'node:fs';
import {validateIntelligenceReply} from './frontend/src/generalIntelligence.js';
import {intelligenceFixture} from './frontend/src/ui/__tests__/fixtures/general-intelligence.js';
const record = JSON.parse(readFileSync('tests/fixtures/general-question-result-held.json', 'utf8'));
const partial = intelligenceFixture();
partial.request_id = record.request_id;
partial.request_digest = record.request_digest;
partial.usage = record.response.intelligence.usage;
record.response.intelligence = partial;
console.log(JSON.stringify({valid: validateIntelligenceReply(partial, {terminalError: true}).ok, record}));
"""
    generated = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=FIXTURES.parents[1],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    mutation = json.loads(generated.stdout)
    assert mutation["valid"] is True
    value = mutation["record"]
    data = canonical(value)
    with pytest.raises(ResultVerificationError):
        decode(
            data,
            state="held",
            pointer=receipt("held", hashlib.sha256(data).hexdigest()),
        )
    value.update(plan_digest="a" * 64, snapshot_digest="b" * 64)
    data = canonical(value)
    accepted = decode(
        data, state="held", pointer=receipt("held", hashlib.sha256(data).hexdigest())
    )
    assert accepted["response"]["intelligence"]["status"] == "partial"


def test_result_reader_refuses_oversize_instead_of_trimming():
    with pytest.raises(ResultVerificationError):
        decode_result_record(
            raw_fixture(),
            generation="17",
            invocation=EXPECTED,
            receipt=receipt(),
            max_bytes=100,
        )


def test_producer_fixtures_pass_the_existing_consumer_intelligence_validator():
    script = """
import {readFileSync} from 'node:fs';
import {validateIntelligenceReply} from './frontend/src/generalIntelligence.js';
const states = ['unavailable', 'held'];
const checked = states.map(state => {
  const {response} = JSON.parse(readFileSync(`tests/fixtures/general-question-result-${state}.json`, 'utf8'));
  return {state, ok: validateIntelligenceReply(response.intelligence, {terminalError: response.error === true}).ok};
});
console.log(JSON.stringify(checked));
"""
    checked = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=FIXTURES.parents[1],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    assert json.loads(checked.stdout) == [
        {"state": "unavailable", "ok": True},
        {"state": "held", "ok": True},
    ]
