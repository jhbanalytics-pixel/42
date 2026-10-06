"""Unit tests for the literal quotation kernel of the general question lane."""

import hashlib

import pytest
from src.analysis.open_intelligence.general_question_quote_span import quote_span

SPAN_KEYS = {"start", "end", "quote_sha256"}


# fmt: off
# Package test from the Q03 journey, kept byte for byte. The fence keeps the
# formatter from restyling the slice expression the package wrote.
def test_quote_is_exact_and_source_local():
    text = "First claim. Café commuters differ. Final claim."
    value = quote_span(text, "Café commuters differ.")
    assert text[value["start"]:value["end"]] == "Café commuters differ."
    assert value["quote_sha256"] == hashlib.sha256("Café commuters differ.".encode()).hexdigest()
    with pytest.raises(ValueError):
        quote_span(text, "All commuters differ.")
# fmt: on


def test_second_occurrence_is_the_second_literal_match():
    text = "alpha beta alpha beta alpha"
    spans = [quote_span(text, "alpha", occurrence) for occurrence in (0, 1, 2)]
    assert [span["start"] for span in spans] == [0, 11, 22]
    assert [span["end"] for span in spans] == [5, 16, 27]
    for span in spans:
        assert text[span["start"] : span["end"]] == "alpha"
    assert quote_span(text, "alpha")["start"] == 0


def test_occurrence_beyond_available_matches_refuses():
    text = "alpha beta alpha"
    assert quote_span(text, "alpha", 1)["start"] == 11
    with pytest.raises(ValueError):
        quote_span(text, "alpha", 2)
    with pytest.raises(ValueError):
        quote_span(text, "beta", 1)


def test_negative_occurrence_refuses():
    with pytest.raises(ValueError):
        quote_span("alpha beta", "alpha", -1)


def test_non_integer_occurrence_refuses():
    with pytest.raises(ValueError):
        quote_span("alpha beta", "alpha", "0")
    with pytest.raises(ValueError):
        quote_span("alpha beta", "alpha", 0.0)
    with pytest.raises(ValueError):
        quote_span("alpha beta", "alpha", True)


def test_overlapping_candidates_are_counted_non_overlapping():
    text = "aaaa"
    first = quote_span(text, "aa", 0)
    second = quote_span(text, "aa", 1)
    assert (first["start"], first["end"]) == (0, 2)
    assert (second["start"], second["end"]) == (2, 4)
    with pytest.raises(ValueError):
        quote_span(text, "aa", 2)


def test_case_or_trailing_space_difference_refuses():
    text = "First claim. Café commuters differ. Final claim."
    exact = quote_span(text, "Final claim.")
    assert text[exact["start"] : exact["end"]] == "Final claim."
    with pytest.raises(ValueError):
        quote_span(text, "café commuters differ.")
    with pytest.raises(ValueError):
        quote_span(text, "CAFÉ COMMUTERS DIFFER.")
    with pytest.raises(ValueError):
        quote_span(text, "Final claim. ")
    with pytest.raises(ValueError):
        quote_span(text, " First claim.")
    with pytest.raises(ValueError):
        quote_span(text, "Café  commuters differ.")


def test_unicode_normalisation_is_not_applied():
    text = "First claim. Café commuters differ. Final claim."
    decomposed = "Café commuters differ."
    assert decomposed != "Café commuters differ."
    with pytest.raises(ValueError):
        quote_span(text, decomposed)


def test_empty_quote_refuses():
    with pytest.raises(ValueError):
        quote_span("First claim.", "")
    with pytest.raises(ValueError):
        quote_span("", "")


def test_non_string_arguments_refuse():
    with pytest.raises(ValueError):
        quote_span("First claim.", b"First")
    with pytest.raises(ValueError):
        quote_span(b"First claim.", "First")
    with pytest.raises(ValueError):
        quote_span(None, "First")
    with pytest.raises(ValueError):
        quote_span("First claim.", 1)


def test_offsets_count_code_points_not_bytes():
    text = "Ébène \U0001f686 rail. Quote me."
    quote = "Quote me."
    value = quote_span(text, quote)
    assert value["start"] == text.index(quote) == 14
    assert value["end"] == 14 + len(quote) == 23
    assert text[value["start"] : value["end"]] == quote
    assert len(text[: value["start"]].encode("utf-8")) == 19
    assert len(text[: value["start"]].encode("utf-16-le")) // 2 == 15


def test_returned_dict_has_exactly_three_keys():
    value = quote_span("First claim. Final claim.", "Final claim.")
    assert set(value) == SPAN_KEYS
    assert len(value) == 3
    assert isinstance(value["start"], int)
    assert isinstance(value["end"], int)
    assert value["quote_sha256"] == hashlib.sha256(b"Final claim.").hexdigest()
    assert len(value["quote_sha256"]) == 64
