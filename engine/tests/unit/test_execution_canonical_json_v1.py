"""Contract proof for native execution JSON canonicality and policy storage."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest
from scripts.migrations import create_open_intelligence_execution_approval_store as migration
from src.analysis.open_intelligence import execution_approval

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "infra" / "bigquery_schemas"
ROUTINES = ROOT / "infra" / "bigquery_routines"
POLICY_SCHEMA = SCHEMAS / "open_intelligence_execution_origin_policies_v1.sql"
UDF_SQL = ROUTINES / "fn_is_canonical_execution_json_v1.sql"
UNICODE_CORPUS = ROOT / "tests" / "fixtures" / "unicode" / "NormalizationTest-15.1.0.txt.gz"
UNICODE_LICENSE = ROOT / "docs" / "licenses" / "Unicode-LICENSE.txt"

POLICY_FIELDS = (
    ("contract_sha256", "STRING", "REQUIRED", "Immutable origin policy digest."),
    ("canonical_policy_json", "STRING", "REQUIRED", "Complete canonical origin policy JSON."),
    ("registered_at", "TIMESTAMP", "REQUIRED", "BigQuery server-owned registration time."),
    ("registered_by", "STRING", "REQUIRED", "Registering control-plane identity."),
)
PROPOSAL_HASHES = {
    "execution_origins_v1.json": "d67a0f738b25fbf95bc7e79d376a5043b95ed49c600f083771a6566f2b6fc179",
    "resource_manifest.json": "33ed5155608920fddc36e199e77a3082b381b65c3dcd0d803f201811571f1e09",
    "successor-brain-read.json": "596920b6dd5d3d431120349af7b108ab3182337e008c449ebee1557ea69f2de4",
    "successor-migration-exposure.json": "0fd42bbc35e9db6e935f6fb353f7956fb6d8c433999c563285a35d96c96a9a65",
    "successor-source-snapshot-capture.json": (
        "66e1a11f30c6dd8a7ca9db2110ef0af91d9a3aeda6b48d0b6323797839a06546"
    ),
    "successor-wave1-pilot.json": "edc805068e0297085926004b86fb1d7d30cc2158e07761439cdb6a6ed074c2d3",
}
UNICODE_15_1_REGRESSIONS = (
    "𐗉",
    "𐗤",
    "𑎃",
    "𑎅",
    "𑎎",
    "𑎑",
    "𑏅",
    "𑏇",
    "𑏈",
    "𖄡",
    "𖄢",
    "𖄣",
    "𖄤",
    "𖄥",
    "𖄦",
    "𖄧",
    "𖄨",
    "𖵨",
    "𖵩",
    "𖵪",
)


def _python_oracle(raw: str) -> bool:
    try:
        execution_approval._canonical_json_object(raw, "canonical_control")
    except (execution_approval.ApprovalRefusal, RecursionError, UnicodeError, ValueError):
        return False
    return True


def _udf_body() -> str:
    sql = UDF_SQL.read_text(encoding="utf-8")
    match = re.search(r'AS r"""\n(?P<body>.*)\n""";\s*$', sql, re.DOTALL)
    assert match is not None, "missing literal persistent JavaScript UDF body"
    return match.group("body")


def _node_binary() -> Path:
    override = os.environ.get("NODE_BINARY")
    if override is not None:
        candidate = Path(override)
        assert candidate.is_file(), f"NODE_BINARY is not a file: {candidate}"
        return candidate
    discovered = shutil.which("node")
    assert discovered is not None, "Node is required: set NODE_BINARY or place node on PATH"
    return Path(discovered)


def _node_results(tmp_path: Path, values: list[str]) -> list[bool]:
    body = _udf_body()
    runner = tmp_path / "run_udf.js"
    runner.write_text(
        "const fs = require('fs');\n"
        "const values = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        "function invoke(raw) {\n"
        f"{body}\n"
        "}\n"
        "process.stdout.write(JSON.stringify(values.map(invoke)));\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [_node_binary(), runner],
        input=json.dumps(values, ensure_ascii=False),
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _nested_payload(container_depth: int) -> str:
    assert container_depth >= 1
    return '{"v":' + "[" * (container_depth - 1) + "0" + "]" * (container_depth - 1) + "}"


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        assert position < len(data), "truncated Unicode varint"
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, position
        shift += 7
        assert shift <= 35, "oversized Unicode varint"


def _decode_pinned_data() -> tuple[dict[int, int], dict[int, tuple[tuple[int, ...], bool]]]:
    match = re.search(r"var PINNED_NFC_DATA = '([^']+)';", _udf_body())
    assert match is not None, "missing pinned Unicode 15.1 data"
    encoded = match.group(1).encode("ascii")
    assert hashlib.sha256(encoded).hexdigest() == (
        "68d6054229792a3be8ca24fa7a435cd4ccb8579d7752aa24146c32b25982a361"
    )
    data = base64.b85decode(encoded)
    assert len(data) == 13545
    assert hashlib.sha256(data).hexdigest() == (
        "3349fa1a93ab11c06373e2666ebb94f4df34a2127e20788bdcfbc2404f4f58e9"
    )

    position = 0
    version, position = _read_varint(data, position)
    assert version == 1
    class_count, position = _read_varint(data, position)
    classes: dict[int, int] = {}
    codepoint = 0
    for _ in range(class_count):
        delta, position = _read_varint(data, position)
        combining_class, position = _read_varint(data, position)
        codepoint += delta
        classes[codepoint] = combining_class

    decomposition_count, position = _read_varint(data, position)
    decompositions: dict[int, tuple[tuple[int, ...], bool]] = {}
    codepoint = 0
    for _ in range(decomposition_count):
        delta, position = _read_varint(data, position)
        codepoint += delta
        length_and_flag, position = _read_varint(data, position)
        length = length_and_flag >> 1
        composition_enabled = bool(length_and_flag & 1)
        parts = []
        for _ in range(length):
            encoded_difference, position = _read_varint(data, position)
            difference = (
                encoded_difference // 2
                if encoded_difference % 2 == 0
                else -(encoded_difference // 2) - 1
            )
            parts.append(codepoint + difference)
        decompositions[codepoint] = (tuple(parts), composition_enabled)

    assert position == len(data)
    assert (len(classes), len(decompositions)) == (922, 2061)
    assert sum(enabled for _parts, enabled in decompositions.values()) == 941
    return classes, decompositions


def _codepoints(value: str) -> tuple[int, ...]:
    return tuple(ord(character) for character in value)


def _normalization_corpus() -> list[tuple[str, str, str, str, str]]:
    compressed = UNICODE_CORPUS.read_bytes()
    assert hashlib.sha256(compressed).hexdigest() == (
        "216118f25a9bb072f4414796bd6acc5488b3114337d12333cbf01079d48966c0"
    )
    raw = gzip.decompress(compressed)
    assert hashlib.sha256(raw).hexdigest() == (
        "871238e37e3be0696ec2bd0891119a041b052da1a84485eda05a5438724b223e"
    )
    rows = []
    for line in raw.decode("utf-8").splitlines():
        content = line.split("#", 1)[0].strip()
        if not content or content.startswith("@"):
            continue
        fields = content.split(";")[:5]
        assert len(fields) == 5
        rows.append(
            tuple("".join(chr(int(token, 16)) for token in field.split()) for field in fields)
        )
    assert len(rows) == 19074
    return rows


def test_policy_registration_schema_has_exact_required_layout():
    sql = POLICY_SCHEMA.read_text(encoding="utf-8")

    assert migration._schema_from_sql(sql) == POLICY_FIELDS
    assert "`{project}.{dataset}.open_intelligence_execution_origin_policies_v1`" in sql
    for forbidden in (
        "INSERT ",
        "MERGE ",
        "PARTITION BY",
        "CLUSTER BY",
        "PRIMARY KEY",
        "UNIQUE",
        "DEFAULT ",
        "GRANT ",
    ):
        assert forbidden not in sql.upper()


def test_delivered_udf_has_exact_pure_persistent_interface():
    sql = UDF_SQL.read_text(encoding="utf-8")

    assert (
        "CREATE OR REPLACE FUNCTION "
        "`{project}.{dataset}.fn_is_canonical_execution_json_v1`(raw STRING)\n"
        "RETURNS BOOL\nLANGUAGE js" in sql
    )
    assert "raw STRING" in sql
    for forbidden in ("require(", "fetch(", "XMLHttpRequest", "BigQuery", "eval("):
        assert forbidden not in _udf_body()


def test_delivered_udf_embeds_exact_python_unicode_15_1_properties():
    assert unicodedata.unidata_version == "15.1.0"
    classes, decompositions = _decode_pinned_data()
    assert not any(0xAC00 <= codepoint <= 0xD7A3 for codepoint in decompositions)

    for codepoint, combining_class in classes.items():
        assert unicodedata.combining(chr(codepoint)) == combining_class
    for codepoint, (parts, composition_enabled) in decompositions.items():
        raw_decomposition = unicodedata.decomposition(chr(codepoint))
        assert raw_decomposition
        assert not raw_decomposition.startswith("<")
        assert tuple(int(token, 16) for token in raw_decomposition.split()) == parts
        expected_composition = len(parts) == 2 and unicodedata.normalize(
            "NFC", "".join(chr(part) for part in parts)
        ) == chr(codepoint)
        assert composition_enabled is expected_composition


def test_delivered_udf_body_stays_inline_bounded_and_ambient_normalization_free():
    body = _udf_body()

    assert len(body.encode("utf-8")) <= 32000
    assert ".normalize(" not in body
    assert "PINNED_NFC_DATA" in body


def test_delivered_udf_matches_existing_python_canonical_oracle(tmp_path: Path):
    valid = [
        "{}",
        '{"a":null}',
        '{"a":false,"b":true}',
        '{"a":[null,true,false,"x",{"b":2}]}',
        '{"v":"quote\\" slash/ backslash\\\\ controls\\b\\f\\n\\r\\t\\u0001"}',
        f'{{"v":"é 한글 {chr(0x2028)} {chr(0x2029)}"}}',
        '{"\ue000":1,"𐀀":2}',
        '{"v":-9223372036854775808}',
        '{"v":9223372036854775807}',
        '{"v":9223372036854775808}',
        '{"v":9007199254740991}',
        '{"v":9007199254740992}',
        '{"v":9007199254740993}',
        '{"v":-0.0}',
        '{"v":0.0}',
        '{"v":1.0}',
        '{"v":1e-05}',
        '{"v":0.0001}',
        '{"v":1000000000000000.0}',
        '{"v":1e+16}',
        '{"v":5e-324}',
        '{"v":1.7976931348623157e+308}',
    ]
    invalid = [
        "",
        " ",
        "[]",
        '"value"',
        "null",
        "true",
        "1",
        " {}",
        "{}\n",
        '{"a" :1}',
        '{"a": 1}',
        '{"a":1, "b":2}',
        '{"b":1,"a":2}',
        '{"a":{"x":1,"x":2}}',
        '{"a":1,"a":2}',
        '{"a":1,"\\u0061":2}',
        '{"v":"\\u00e9"}',
        '{"v":"a\\/b"}',
        '{"v":"é"}',
        '{"𐀀":2,"\ue000":1}',
        '{"v":"\\ud800"}',
        '{"v":"\\udc00"}',
        '{"v":-0}',
        '{"v":01}',
        '{"v":1.}',
        '{"v":.1}',
        '{"v":1e0}',
        '{"v":1e-5}',
        '{"v":1e-04}',
        '{"v":0.00001}',
        '{"v":1e16}',
        '{"v":1e+016}',
        '{"v":1e309}',
        '{"v":NaN}',
        '{"v":Infinity}',
    ]
    values = valid + invalid
    expected = [True] * len(valid) + [False] * len(invalid)

    assert [_python_oracle(value) for value in values] == expected
    assert _node_results(tmp_path, values) == expected


@pytest.mark.parametrize("value", UNICODE_15_1_REGRESSIONS)
def test_unicode_15_1_regression_is_accepted_by_delivered_udf(tmp_path: Path, value: str):
    raw = json.dumps({"v": value}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    assert _python_oracle(raw)
    assert _node_results(tmp_path, [raw]) == [True]


def test_pinned_nfc_handles_hangul_exclusions_blocking_and_unknown_codepoints(tmp_path: Path):
    values = (
        "가",
        "가",
        "각",
        "각",
        "\u0308\u0301",
        "\u0344",
        "A\u0305\u0300",
        "À\u0305",
        "A\u0327\u0301",
        "Á\u0327",
        "\U000105c9",
    )
    raw_values = [
        json.dumps({"v": value}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for value in values
    ]
    expected = [unicodedata.normalize("NFC", value) == value for value in values]

    assert expected == [True, False, True, False, True, False, True, True, False, True, True]
    assert _node_results(tmp_path, raw_values) == expected


def test_full_unicode_15_1_normalization_corpus_is_accounted_and_matches(tmp_path: Path):
    rows = _normalization_corpus()
    expectations: dict[str, bool] = {}
    accounted = 0
    for source, nfc, nfd, nfkc, nfkd in rows:
        for value, expected in (
            (source, source == nfc),
            (nfc, True),
            (nfd, nfd == nfc),
            (nfkc, True),
            (nfkd, nfkd == nfkc),
        ):
            accounted += 1
            if value in expectations:
                assert expectations[value] is expected
            else:
                expectations[value] = expected
    assert accounted == 19074 * 5
    assert len(expectations) == 36482
    values = [
        json.dumps({"v": value}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for value in expectations
    ]

    assert _node_results(tmp_path, values) == list(expectations.values())


def test_unicode_public_assets_are_exact():
    assert hashlib.sha256(UNICODE_LICENSE.read_bytes()).hexdigest() == (
        "e7a93b009565cfce55919a381437ac4db883e9da2126fa28b91d12732bc53d96"
    )


def test_node_binary_override_is_explicit_and_never_falls_back(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NODE_BINARY", str(ROOT / "missing-node"))
    monkeypatch.setattr(shutil, "which", lambda _name: "fallback-node")

    with pytest.raises(AssertionError, match="NODE_BINARY is not a file"):
        _node_binary()


def test_node_binary_missing_from_path_fails_clearly(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NODE_BINARY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(AssertionError, match="Node is required"):
        _node_binary()


def test_node_binary_uses_path_discovery_without_override(monkeypatch: pytest.MonkeyPatch):
    discovered = ROOT / "portable-node"
    monkeypatch.delenv("NODE_BINARY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: str(discovered) if name == "node" else None)

    assert _node_binary() == discovered


def test_delivered_udf_matches_deterministic_finite_binary64_oracle(tmp_path: Path):
    rng = random.Random(42)
    values: list[str] = []
    while len(values) < 512:
        value = struct.unpack(">d", rng.getrandbits(64).to_bytes(8, "big"))[0]
        if math.isfinite(value):
            values.append(
                json.dumps({"v": value}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )

    assert all(_python_oracle(value) for value in values)
    assert _node_results(tmp_path, values) == [True] * len(values)


def test_delivered_udf_accepts_exact_reviewed_registry_policy_and_resource_bytes(tmp_path: Path):
    proposal_dir_value = os.environ.get("R03_CANONICAL_PROPOSAL_DIR")
    if proposal_dir_value is None:
        pytest.skip("reviewed proposal packet is external to the source checkout")
    proposal_dir = Path(proposal_dir_value)
    values = []
    for filename, expected_hash in PROPOSAL_HASHES.items():
        raw_bytes = (proposal_dir / filename).read_bytes()
        assert hashlib.sha256(raw_bytes).hexdigest() == expected_hash
        values.append(raw_bytes.decode("utf-8"))

    assert all(_python_oracle(value) for value in values)
    assert _node_results(tmp_path, values) == [True] * len(values)


def test_delivered_udf_enforces_depth_and_integer_digit_boundaries(tmp_path: Path):
    assert sys.get_int_max_str_digits() == 4300
    values = [
        _nested_payload(500),
        _nested_payload(501),
        '{"v":' + "9" * 4300 + "}",
        '{"v":-' + "9" * 4300 + "}",
        '{"v":' + "9" * 4301 + "}",
        '{"v":-' + "9" * 4301 + "}",
    ]

    assert _node_results(tmp_path, values) == [True, False, True, True, False, False]


def test_schema_parser_and_udf_controls_reject_contract_mutations(tmp_path: Path):
    schema_sql = POLICY_SCHEMA.read_text(encoding="utf-8")
    mutated_schema = schema_sql.replace("contract_sha256 STRING NOT NULL", "contract_sha256 INT64")
    with pytest.raises(AssertionError):
        assert migration._schema_from_sql(mutated_schema) == POLICY_FIELDS

    values = ['{"a":1,"a":2}', '{"b":1,"a":2}', '{"v":1e0}']
    assert _node_results(tmp_path, values) == [False, False, False]
