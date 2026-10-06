import hashlib
import json
import socket

import pytest

from ops.evaluation import run_discovery_review as cli
from ops.evaluation.discovery_review import freeze_review_sample, score_review_sample
from ops.tests.test_evaluation_discovery import (
    CLOSED_AT,
    MARKETS,
    OPEN_AT,
    WEEK_START,
    clusters,
    review,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def freeze_input(frozen_at=CLOSED_AT):
    return {
        "week_start": WEEK_START,
        "frozen_at": frozen_at,
        "markets": list(MARKETS),
        "clusters": clusters({"za": 3, "ng": 2}),
    }


def run(tmp_path, command, document, name="receipt.json"):
    source = tmp_path / f"{name}.input.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / name
    code = cli.main([command, "--input", str(source), "--output", str(output)])
    return code, source, output


def test_freeze_writes_the_frozen_sample_bound_to_its_input_bytes(tmp_path):
    code, source, output = run(tmp_path, "freeze", freeze_input())
    assert code == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["command"] == "freeze"
    assert receipt["input_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    document = freeze_input()
    assert receipt["sample"] == freeze_review_sample(
        document["clusters"],
        week_start=WEEK_START,
        frozen_at=CLOSED_AT,
        markets=MARKETS,
    )
    assert receipt["sample"]["markets"]["ke"]["gate"] == "unmet"
    unsealed = dict(receipt)
    assert unsealed.pop("receipt_sha256") == cli._digest(unsealed)


C07 = {
    "minimum_coherence": 0.8,
    "duplication_below": 0.1,
    "maximum_reviewed_foreign_market": 0,
}


def frozen_receipt(tmp_path):
    _code, _source, frozen = run(tmp_path, "freeze", freeze_input(), "frozen.json")
    return frozen, json.loads(frozen.read_text(encoding="utf-8"))["sample"]


def all_reviews(sample, **flags):
    return [
        review(cluster_id, market, **flags.get(cluster_id, {}))
        for market, values in sample["markets"].items()
        for cluster_id in values["cluster_ids"]
    ]


def score(tmp_path, frozen, document, *, thresholds=C07, name="score.json"):
    source = tmp_path / f"{name}.input.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    argv = ["score", "--freeze-receipt", str(frozen), "--input", str(source)]
    limits = None
    if thresholds is not None:
        limits = tmp_path / f"{name}.thresholds.json"
        limits.write_text(json.dumps(thresholds), encoding="utf-8")
        argv += ["--thresholds", str(limits)]
    output = tmp_path / name
    code = cli.main([*argv, "--output", str(output)])
    return code, output, limits


def test_score_binds_the_freeze_receipt_and_the_pinned_c07_thresholds(tmp_path):
    frozen, sample = frozen_receipt(tmp_path)
    reviews = all_reviews(sample, za_000={"coherent": False})
    code, output, limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": reviews}
    )
    assert code == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert (
        receipt["freeze_receipt_sha256"]
        == hashlib.sha256(frozen.read_bytes()).hexdigest()
    )
    assert (
        receipt["thresholds_sha256"] == hashlib.sha256(limits.read_bytes()).hexdigest()
    )
    assert receipt["thresholds"] == C07
    assert "C07" in receipt["thresholds_source"]
    assert receipt["result"] == score_review_sample(sample, reviews)
    assert receipt["result"]["pooled"]["reviewed"] == 5
    assert receipt["gate"]["markets"] == {"za": "unmet", "ng": "met", "ke": "unmet"}
    assert receipt["gate"]["pooled"] == "unmet"


def test_duplication_at_ten_percent_or_any_reviewed_foreign_case_is_not_met(tmp_path):
    frozen, sample = frozen_receipt(tmp_path)
    code, output, _limits = score(
        tmp_path,
        frozen,
        {"sample": sample, "reviews": all_reviews(sample, ng_000={"foreign": True})},
    )
    assert code == 0
    gate = json.loads(output.read_text(encoding="utf-8"))["gate"]
    assert gate["markets"]["ng"] == "unmet"
    assert gate["markets"]["za"] == "met"
    assert gate["pooled"] == "unmet"
    from ops.evaluation.discovery_review import gate_c07_thresholds

    ten = {
        "sampled": 10,
        "reviewed": 10,
        "unreviewed": 0,
        "coherent": 10,
        "duplicate": 1,
        "foreign_market": 0,
        "coherence": 1.0,
        "duplication": 0.1,
    }
    pooled = {
        **{key: value * 3 for key, value in ten.items() if type(value) is int},
        "coherence": 1.0,
        "duplication": 0.1,
    }
    result = {
        "sample_digest": "x",
        "markets": {market: dict(ten) for market in MARKETS},
        "pooled": pooled,
        "complete": True,
    }
    assert gate_c07_thresholds(result)["markets"]["za"] == "unmet"


def test_score_without_thresholds_says_so_and_emits_no_gate(tmp_path):
    frozen, sample = frozen_receipt(tmp_path)
    code, output, _limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": []}, thresholds=None
    )
    assert code == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["gate"] is None
    assert receipt["thresholds"] is None
    assert receipt["thresholds_sha256"] is None
    assert receipt["result"]["pooled"]["coherence"] is None


@pytest.mark.parametrize(
    "thresholds",
    [
        {**C07, "minimum_coherence": 0.5},
        {**C07, "duplication_below": 0.2},
        {**C07, "maximum_reviewed_foreign_market": 1},
        {"minimum_coherence": 0.8, "maximum_duplication": 0.1},
    ],
)
def test_thresholds_other_than_c07_are_refused(tmp_path, thresholds):
    frozen, sample = frozen_receipt(tmp_path)
    code, output, _limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": []}, thresholds=thresholds
    )
    assert code == 2
    assert not output.exists()


def test_score_refuses_a_sample_that_is_not_the_frozen_one(tmp_path):
    frozen, sample = frozen_receipt(tmp_path)
    other = freeze_review_sample(
        clusters({"za": 1}), week_start=WEEK_START, frozen_at=CLOSED_AT, markets=MARKETS
    )
    assert other["sample_digest"] != sample["sample_digest"]
    code, output, _limits = score(tmp_path, frozen, {"sample": other, "reviews": []})
    assert code == 2
    assert not output.exists()


def test_score_refuses_an_edited_freeze_receipt(tmp_path):
    frozen, sample = frozen_receipt(tmp_path)
    receipt = json.loads(frozen.read_text(encoding="utf-8"))
    receipt["input_sha256"] = "0" * 64
    frozen.write_text(json.dumps(receipt), encoding="utf-8")
    code, output, _limits = score(tmp_path, frozen, {"sample": sample, "reviews": []})
    assert code == 2
    assert not output.exists()
    receipt = cli.build_receipt("freeze", json.dumps(freeze_input()).encode("utf-8"))
    receipt["command"] = "score"
    receipt["receipt_sha256"] = cli._digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    frozen.write_text(json.dumps(receipt), encoding="utf-8")
    code, output, _limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": []}, name="relabelled.json"
    )
    assert code == 2
    assert not output.exists()


def test_refusals_write_nothing(tmp_path):
    code, _source, output = run(tmp_path, "freeze", freeze_input(OPEN_AT))
    assert code == 2
    assert not output.exists()
    extra = {**freeze_input(), "extra": 1}
    code, _source, output = run(tmp_path, "freeze", extra, "extra.json")
    assert code == 2
    assert not output.exists()
    frozen, sample = frozen_receipt(tmp_path)
    sample["markets"]["za"]["sampled"] = 1
    code, output, _limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": []}, name="moved.json"
    )
    assert code == 2
    assert not output.exists()
    code, output, _limits = score(
        tmp_path,
        frozen,
        {"sample": frozen_receipt(tmp_path)[1], "reviews": [], "thresholds": None},
        name="inline.json",
    )
    assert code == 2
    assert not output.exists()


def test_existing_receipt_is_never_overwritten(tmp_path):
    output = tmp_path / "receipt.json"
    output.write_text("kept", encoding="utf-8")
    code, _source, _output = run(tmp_path, "freeze", freeze_input())
    assert code == 2
    assert output.read_text(encoding="utf-8") == "kept"


def test_a_promised_market_with_no_clusters_is_unmet_and_never_complete(tmp_path):
    frozen, sample = frozen_receipt(tmp_path)
    assert sample["markets"]["ke"]["sampled"] == 0
    code, output, _limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": all_reviews(sample)}
    )
    assert code == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["unobserved_markets"] == ["ke"]
    assert receipt["result"]["complete"] is False
    gate = receipt["gate"]
    assert gate["markets"] == {"za": "met", "ng": "met", "ke": "unmet"}
    assert gate["pooled"] == "unmet"
    assert gate["complete"] is False


def test_generic_gate_reads_an_unobserved_promised_market_as_unmet():
    from ops.evaluation.discovery_review import gate_review_thresholds

    sample = freeze_review_sample(
        clusters({"za": 2, "ng": 1}),
        week_start=WEEK_START,
        frozen_at=CLOSED_AT,
        markets=MARKETS,
    )
    reviews = all_reviews(sample)
    result = score_review_sample(sample, reviews)
    assert result["complete"] is False
    gate = gate_review_thresholds(
        result, minimum_coherence=0.8, maximum_duplication=0.1
    )
    assert gate["markets"]["ke"] == "unmet"
    assert gate["pooled"] == "unmet"
    assert gate["complete"] is False


@pytest.mark.parametrize(
    "markets", [["za"], ["za", "ng"], ["ke", "ng", "za", "gh"], []]
)
def test_freeze_promises_exactly_the_product_markets(tmp_path, markets):
    document = {**freeze_input(), "markets": markets}
    document["clusters"] = [
        row for row in document["clusters"] if row["market"] in markets
    ]
    code, _source, output = run(tmp_path, "freeze", document)
    assert code == 2
    assert not output.exists()


def test_freeze_accepts_the_product_markets_in_any_order(tmp_path):
    code, _source, output = run(
        tmp_path, "freeze", {**freeze_input(), "markets": ["ke", "za", "ng"]}
    )
    assert code == 0
    assert set(
        json.loads(output.read_text(encoding="utf-8"))["sample"]["markets"]
    ) == set(MARKETS)


def test_score_refuses_a_forged_freeze_receipt_over_a_narrowed_market_set(tmp_path):
    sample = freeze_review_sample(
        clusters({"za": 1}), week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
    )
    body = {
        "receipt_version": cli.RECEIPT_VERSION,
        "command": "freeze",
        "input_sha256": "0" * 64,
        "sample": sample,
    }
    frozen = tmp_path / "forged.json"
    frozen.write_text(
        json.dumps({**body, "receipt_sha256": cli._digest(body)}), encoding="utf-8"
    )
    code, output, _limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": all_reviews(sample)}
    )
    assert code == 2
    assert not output.exists()
    from ops.evaluation.discovery_review import gate_c07_thresholds

    with pytest.raises(ValueError, match="markets_not_promised"):
        gate_c07_thresholds(score_review_sample(sample, all_reviews(sample)))


def test_score_without_a_freeze_receipt_is_a_clean_refusal(tmp_path, capsys):
    source = tmp_path / "score.input.json"
    source.write_text(json.dumps({"sample": {}, "reviews": []}), encoding="utf-8")
    output = tmp_path / "receipt.json"
    code = cli.main(["score", "--input", str(source), "--output", str(output)])
    assert code == 2
    assert not output.exists()
    assert capsys.readouterr().err.strip() == "freeze_receipt_required"
    with pytest.raises(ValueError, match="freeze_receipt_required"):
        cli.build_receipt("score", source.read_bytes())


@pytest.mark.parametrize(
    "thresholds",
    [
        {**C07, "maximum_reviewed_foreign_market": 0.0},
        {**C07, "maximum_reviewed_foreign_market": False},
        {**C07, "minimum_coherence": "0.8"},
        [C07],
    ],
)
def test_thresholds_of_the_wrong_type_are_refused_even_when_equal(tmp_path, thresholds):
    frozen, sample = frozen_receipt(tmp_path)
    code, output, _limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": []}, thresholds=thresholds
    )
    assert code == 2
    assert not output.exists()
    with pytest.raises(ValueError, match="thresholds_not_c07"):
        cli._pinned_thresholds(json.dumps(thresholds).encode("utf-8"))


def full_freeze_input(counts):
    return {
        "week_start": WEEK_START,
        "frozen_at": CLOSED_AT,
        "markets": list(MARKETS),
        "clusters": clusters(counts),
    }


def test_one_failing_market_fails_the_overall_c07_verdict_while_the_pool_is_met(
    tmp_path,
):
    _code, _source, frozen = run(
        tmp_path,
        "freeze",
        full_freeze_input({"za": 30, "ng": 30, "ke": 30}),
        "full.json",
    )
    sample = json.loads(frozen.read_text(encoding="utf-8"))["sample"]
    incoherent = {f"ke_{index:03d}": {"coherent": False} for index in range(10)}
    reviews = all_reviews(sample, **incoherent)
    code, output, _limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": reviews}
    )
    assert code == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    gate = receipt["gate"]
    assert gate["markets"] == {"za": "met", "ng": "met", "ke": "unmet"}
    assert gate["pooled"] == "met"
    assert gate["complete"] is True
    assert gate["verdict"] == "unmet"
    assert receipt["verdict"] == "unmet"


def test_every_market_met_gives_a_met_overall_verdict(tmp_path):
    _code, _source, frozen = run(
        tmp_path, "freeze", full_freeze_input({"za": 2, "ng": 2, "ke": 2}), "full.json"
    )
    sample = json.loads(frozen.read_text(encoding="utf-8"))["sample"]
    code, output, _limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": all_reviews(sample)}
    )
    assert code == 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["gate"]["verdict"] == "met"
    assert receipt["verdict"] == "met"


def test_without_thresholds_the_receipt_verdict_is_no_gate(tmp_path):
    frozen, sample = frozen_receipt(tmp_path)
    code, output, _limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": []}, thresholds=None
    )
    assert code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["verdict"] == "no_gate"


def test_pool_at_exactly_ten_percent_duplication_is_unmet():
    from ops.evaluation.discovery_review import (
        gate_c07_thresholds,
        gate_review_thresholds,
    )

    ten = {
        "sampled": 10,
        "reviewed": 10,
        "unreviewed": 0,
        "coherent": 10,
        "duplicate": 1,
        "foreign_market": 0,
        "coherence": 1.0,
        "duplication": 0.1,
    }
    pooled = {
        **{key: value * 3 for key, value in ten.items() if type(value) is int},
        "coherence": 1.0,
        "duplication": 0.1,
    }
    result = {
        "sample_digest": "x",
        "markets": {market: dict(ten) for market in MARKETS},
        "pooled": pooled,
        "complete": True,
    }
    plain = gate_review_thresholds(
        result, minimum_coherence=0.8, maximum_duplication=0.1
    )
    assert plain["pooled"] == "met"
    gate = gate_c07_thresholds(result)
    assert gate["pooled"] == "unmet"
    assert gate["markets"] == {market: "unmet" for market in MARKETS}
    assert gate["verdict"] == "unmet"


def test_score_refuses_a_self_sealed_two_market_freeze_receipt_without_thresholds(
    tmp_path,
):
    sample = freeze_review_sample(
        clusters({"za": 1, "ng": 1}),
        week_start=WEEK_START,
        frozen_at=CLOSED_AT,
        markets=("za", "ng"),
    )
    body = {
        "receipt_version": cli.RECEIPT_VERSION,
        "command": "freeze",
        "input_sha256": "0" * 64,
        "sample": sample,
    }
    frozen = tmp_path / "two.json"
    frozen.write_text(
        json.dumps({**body, "receipt_sha256": cli._digest(body)}), encoding="utf-8"
    )
    code, output, _limits = score(
        tmp_path, frozen, {"sample": sample, "reviews": []}, thresholds=None
    )
    assert code == 2
    assert not output.exists()
    with pytest.raises(ValueError, match="markets_not_promised"):
        cli.build_receipt(
            "score",
            json.dumps({"sample": sample, "reviews": []}).encode("utf-8"),
            freeze_raw=frozen.read_bytes(),
        )


def test_overall_verdict_puts_unmet_ahead_of_incomplete_and_unknown():
    from ops.evaluation.discovery_review import _overall_verdict

    assert _overall_verdict(("met", "met", "met", "met")) == "met"
    assert _overall_verdict(("incomplete", "unmet", "met", "incomplete")) == "unmet"
    assert _overall_verdict(("unknown", "incomplete", "unmet", "met")) == "unmet"
    assert _overall_verdict(("met", "incomplete", "unknown", "met")) == "incomplete"
    assert _overall_verdict(("met", "unknown", "met", "met")) == "unknown"
