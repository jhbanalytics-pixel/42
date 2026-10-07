import copy

import pytest

from core.agent import plain, writer


@pytest.mark.parametrize("source,expected", [
    ("Posted on 2026-10-05.", "Posted on 5 Oct."),
    ("From 2025-12-31 to 2026-01-02.", "From 31 Dec 2025 to 2 Jan."),
    ("Posted on 2024-02-29, not 2026-02-29.", "Posted on 29 Feb 2024, not 2026-02-29."),
    ('On 2026-10-05 they said "meet on 2026-10-04".', 'On 5 Oct they said "meet on 2026-10-04".'),
    ("On 2026-10-05 see https://example.com/2026-10-04?q=2026-10-03.",
     "On 5 Oct see https://example.com/2026-10-04?q=2026-10-03."),
    ("On 2026-10-05 [2026-10-04](https://example.com) [2026-10-03] `2026-10-02`.",
     "On 5 Oct [2026-10-04](https://example.com) [2026-10-03] `2026-10-02`."),
    ("At 2026-10-05T12:30:00Z and 2026-10-05 12:30+02:00, run_2026-10-05 stays.",
     "At 2026-10-05T12:30:00Z and 2026-10-05 12:30+02:00, run_2026-10-05 stays."),
    ("On 2026-10-05, ‘meet on 2026-10-04’ and 'meet on 2026-10-03' stay.",
     "On 5 Oct, ‘meet on 2026-10-04’ and 'meet on 2026-10-03' stay."),
])
def test_ask_presentation_changes_only_unprotected_valid_dates(source, expected):
    answer = {"as_of": "2026-10-07T06:00:00+02:00", "short_answer": source, "gaps": []}
    assert plain.answer(answer)["short_answer"] == expected


def test_presentation_keeps_year_when_reference_is_missing():
    assert plain.answer({"short_answer": "On 2026-10-05.", "gaps": []})["short_answer"] == "On 5 Oct 2026."


def test_presentation_preserves_evidence_quotes_numbers_ids_and_original_answer():
    quote = {"evidence_id": "post_2026-10-05", "text": "meet on 2026-10-04"}
    number = {"value": 5, "unit": "posts on 2026-10-05", "query_id": "q_2026-10-05"}
    answer = {
        "as_of": "2026-10-07T06:00:00+02:00", "short_answer": "Posted on 2026-10-05.",
        "claims": [{"id": "c1", "text": "On 2026-10-05 they wrote meet on 2026-10-04.",
                    "evidence_ids": [quote["evidence_id"]], "quotes": [quote], "numbers": [number]}],
        "evidence": [{"id": quote["evidence_id"], "text": quote["text"], "posted_at": "2026-10-05T12:00:00Z",
                      "url": "https://example.com/2026-10-05"}],
        "context": "Seen on 2025-10-05.", "so_what": [{"text": "Check 2026-10-05.", "claim_ids": ["c1"]}],
        "watch_next": [{"text": "Compare 2026-10-06.", "claim_ids": ["c1"]}],
        "gaps": [{"what": "No earlier than 2026-10-01.", "searched": "Since 2026-10-01.", "why": "Missing 2026-10-02."}],
    }
    original = copy.deepcopy(answer)
    shown = plain.answer(answer)
    assert shown["claims"][0]["text"] == "On 5 Oct they wrote meet on 2026-10-04."
    assert shown["context"] == "Seen on 5 Oct 2025."
    assert shown["so_what"][0]["text"] == "Check 5 Oct."
    assert shown["watch_next"][0]["text"] == "Compare 6 Oct."
    assert shown["gaps"][0] == {"what": "No earlier than 1 Oct.", "searched": "Since 1 Oct.", "why": "Missing 2 Oct."}
    assert shown["claims"][0]["quotes"] == [quote]
    assert shown["claims"][0]["numbers"] == [number]
    assert shown["evidence"] == answer["evidence"]
    assert shown["as_of"] == answer["as_of"]
    assert answer == original


def test_ask_writer_requests_plain_dates_without_changing_quoted_dates():
    assert "5 Oct" in writer.WRITER_SYSTEM
    assert "ISO" in writer.WRITER_SYSTEM
    assert "year" in writer.WRITER_SYSTEM


@pytest.mark.parametrize("source", [
    "See [archive](/reports?date=2026-10-04).",
    "See [archive](reports?date=2026-10-04).",
    "See [archive](/reports(archive)?date=2026-10-04).",
    "See ftp://example.com?date=2026-10-04.",
    "See sftp://example.com/archive/2026-10-04.",
    "Inspect event.2026-10-04.12.",
    "Inspect 2026-10-04.12.",
    "Inspect event.2026-10-04.",
    "Read report?date=2026-10-04.",
    "See [archive](\n2026-10-04\n).",
])
def test_v2_links_and_machine_tokens_are_never_date_prose(source):
    answer = {"as_of": "2026-10-07", "short_answer": source, "gaps": []}
    assert plain.answer(answer)["short_answer"] == source


def test_v2_standalone_prose_date_after_a_line_break_is_formatted():
    answer = {"as_of": "2026-10-07", "short_answer": "Posted on\n2026-10-05.", "gaps": []}
    assert plain.answer(answer)["short_answer"] == "Posted on\n5 Oct."


@pytest.mark.parametrize("span", [
    '"meet\non 2026-10-04"',
    '"meet\t on 2026-10-04"',
    '“meet\ron 2026-10-04”',
    '＂meet on 2026-10-04＂',
    '"ｍｅｅｔ on 2026-10-04"',
    'meet\non 2026-10-04',
])
def test_v2_verified_quote_spans_stay_exact_through_real_k1(span):
    from core.trust.claims import _verified_quotes, check_answer

    record = {"id": "post_1", "text": "meet on 2026-10-04", "posted_at": "2026-10-04T12:00:00+02:00",
              "handle": "@one", "platform": "tiktok", "market": "ZA", "flags": []}
    quote = {"evidence_id": "post_1", "text": record["text"]}
    claim = {"id": "c1", "text": f"On 2026-10-05 they wrote {span}.", "label": "single_source",
             "evidence_ids": ["post_1"], "quotes": [quote], "numbers": []}
    answer = {"as_of": "2026-10-07T12:00:00+02:00", "short_answer": claim["text"], "claims": [claim],
              "evidence": [record], "gaps": []}
    window = {"window_start": "2026-10-01T00:00:00+02:00", "window_end": "2026-10-07T23:59:59+02:00",
              "market": "ZA"}
    checked, rows = check_answer(answer, **window)
    assert checked["claims"] and not [row for row in rows if row["rule"] == "K1" and row["verdict"] != "pass"]
    original = copy.deepcopy(checked)
    shown = plain.answer(checked)
    assert shown["claims"][0]["text"] == f"On 5 Oct they wrote {span}."
    assert shown["short_answer"] == f"On 5 Oct they wrote {span}."
    assert shown["claims"][0]["quotes"] == [quote]
    assert _verified_quotes(shown["claims"][0], {"post_1": record}) == {record["text"]}
    rechecked, rows = check_answer(shown, **window)
    assert rechecked["claims"] and not [row for row in rows if row["rule"] == "K1" and row["verdict"] != "pass"]
    assert checked == original


@pytest.mark.parametrize("span", [
    '"meet\non 2026-10-04 outside"',
    '“meet on 2026-10-04 outside”',
    '«meet on 2026-10-04 outside»',
    '‹meet on 2026-10-04 outside›',
])
def test_v2_ask_k1_and_k8_verified_spans_stay_exact_without_quote_entries(span):
    from core.agent import checks

    record = {"id": "post_1", "text": "meet on 2026-10-04 outside", "posted_at": "2026-10-04T12:00:00+02:00",
              "handle": "@one", "platform": "tiktok", "market": "ZA", "url": "https://example.com/post_1", "flags": []}
    claim = {"id": "c1", "text": f"On 2026-10-05 they wrote {span}.", "label": "single_source",
             "evidence_ids": ["post_1"], "quotes": [], "numbers": []}
    records = {"post_1": record}
    assert checks._k1(claim, ["post_1"], records, records) == []
    assert checks._k8(claim["text"], [record]) == []
    shown = plain.answer({"as_of": "2026-10-07", "claims": [claim], "gaps": []})
    assert shown["claims"][0]["text"] == f"On 5 Oct they wrote {span}."
    assert checks._k1(shown["claims"][0], ["post_1"], records, records) == []
    assert checks._k8(shown["claims"][0]["text"], [record]) == []
