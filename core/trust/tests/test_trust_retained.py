"""What claim_checks keeps of a failed support or sentence check (W8-DEC-14): a digest of the rejected span and a
reason code from a fixed enum, and never the span, a post or the model's own words."""

import re

from core.trust import retained

CLAIM = 'Creators post the "shaya step" dance, 31 creators in three days.'
CLAIM_SHA = "80fc228ba150a910acc3aad57abf08607197f399e1fc7b5bd805c9bdeacfec44"
FINE = "Fine cafe"
FINE_SHA = "875158553ff3163e1e8b3bdfd7b889149f8316ed144379197200be7ded87b100"


def test_the_digest_is_the_sha256_of_the_span_pinned():
    assert retained.span_sha256(CLAIM) == CLAIM_SHA


def test_nfkc_forms_and_any_whitespace_hash_alike():
    assert retained.span_sha256("Fine　cafe") == FINE_SHA
    assert retained.span_sha256("Fine  cafe\n") == FINE_SHA
    assert retained.span_sha256("  Fine \t cafe  ") == FINE_SHA
    assert retained.span_sha256("Ｆｉｎｅ cafe") == FINE_SHA


def test_a_different_span_or_case_hashes_differently():
    assert retained.span_sha256("fine cafe") != FINE_SHA
    assert retained.span_sha256("Fine cafes") != FINE_SHA


def test_normalise_span_is_nfkc_then_single_spaces():
    assert retained.normalise_span("a  b\r\nc") == "a b c"
    assert retained.normalise_span(None) == ""


def test_no_span_has_no_digest():
    assert retained.span_sha256(None) is None
    assert retained.span_sha256(" \n\t ") is None
    assert retained.span_sha256(12) is None


def test_the_enum_is_a_fixed_set_of_lowercase_codes():
    assert isinstance(retained.REASON_CODES, frozenset)
    assert retained.REASON_CODES
    assert all(re.fullmatch(r"[a-z_]{3,40}", code) for code in retained.REASON_CODES)


def test_a_digest_is_sixty_four_lowercase_hex_characters():
    assert retained.is_digest(CLAIM_SHA)
    for bad in (CLAIM_SHA.upper(), CLAIM_SHA[:-1], CLAIM_SHA + "0", "the post said hello", None, 5, ""):
        assert not retained.is_digest(bad)
