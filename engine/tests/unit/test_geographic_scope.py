"""Geographic scope kernel: retained fixtures and the policies it must refuse.

Every fixture resolves through the real kernel over the real collision policy
(src.utils.geo_blocklist) and the real retained geography rule
(candidates.resolve_wave1_market). Each counterexample names a policy that reads
plausibly and is wrong, and proves the kernel cannot express it.
"""

import dataclasses
import itertools
import math

import pytest
from src.analysis.open_intelligence.geographic_scope import (
    METHOD_CONFIDENCE,
    METHOD_SCOPES,
    METHODS,
    ROW_CITING_METHODS,
    SCOPES,
    TEXT_BYTE_LIMIT,
    TEXT_LIMIT,
    GeographicScope,
    _confidence,
    bounded_name,
    bounded_text,
    local_observation_refs,
    resolve_geographic_scope,
)
from src.utils.geo_blocklist import has_foreign_latin_density

NG_RECEIPT = "geo_receipt_ng_000123"
NG_METHOD = "geo_method_vendor_frame_v1"
LAGOS_NG = "Lagos traffic on Third Mainland Bridge this morning, hold your chest"
LAGOS_PT = "Lagos beach weekend in the Algarve, best cliffs on the coast"
WIRE_STORY = "Central bank holds the policy rate at 27.5%, the third hold this year"
# Cyrillic built from code points so the source stays ASCII. The script
# backstop needs at least five characters from a non SSA block to fire.
CYRILLIC = "".join(chr(code) for code in (0x41F, 0x440, 0x438, 0x432, 0x435, 0x442))
# Turkish built the same way: dotted capital I, g breve, dotless i and two s
# cedillas, five characters where the density backstop needs three.
TURKISH = "".join(
    chr(code)
    for code in (
        0x130,
        0x73,
        0x74,
        0x61,
        0x6E,
        0x62,
        0x75,
        0x6C,
        0x20,
        0x79,
        0x61,
        0x11F,
        0x131,
        0x15F,
        0x20,
        0x76,
        0x65,
        0x20,
        0x15F,
        0x69,
        0x64,
        0x64,
        0x65,
        0x74,
    )
)


def _resolve(**overrides):
    inputs = {"market": "ng", "row_id": "row_ng_0001", "title": LAGOS_NG}
    inputs.update(overrides)
    return resolve_geographic_scope(**inputs)


def _receipt(market="ng", receipt=NG_RECEIPT, method=NG_METHOD):
    return {
        "retained_geo_market": market,
        "geo_method_id": method,
        "geo_receipt_id": receipt,
    }


def test_record_has_exactly_the_four_fields_and_is_immutable():
    assert tuple(field.name for field in dataclasses.fields(GeographicScope)) == (
        "method",
        "evidence_ref",
        "scope",
        "confidence",
    )
    assert frozenset({"local", "contextual", "foreign", "unknown"}) == SCOPES
    assert "none" in METHODS
    assert set(METHOD_CONFIDENCE) == METHODS - {"none"}
    assert all(0.0 < value <= 1.0 for value in METHOD_CONFIDENCE.values())
    record = GeographicScope("retained_geo_receipt", NG_RECEIPT, "local", 0.9)
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.scope = "foreign"


def test_unknown_carries_no_method_evidence_or_confidence():
    record = GeographicScope("none", None, "unknown", None)
    assert record == GeographicScope(
        method="none", evidence_ref=None, scope="unknown", confidence=None
    )
    for bad in (
        {"confidence": 0.0},
        {"method": "content_marker"},
        {"evidence_ref": "row_ng_0001"},
    ):
        with pytest.raises(ValueError, match="unknown scope"):
            GeographicScope(
                **{
                    "method": "none",
                    "evidence_ref": None,
                    "scope": "unknown",
                    "confidence": None,
                    **bad,
                }
            )


def test_resolved_scope_requires_method_evidence_and_bounded_confidence(monkeypatch):
    base = {"method": "content_marker", "evidence_ref": "row_ng_0001", "scope": "contextual"}
    for confidence in (None, -0.1, 1.1, math.nan, True, "0.5"):
        with pytest.raises(ValueError):
            GeographicScope(confidence=confidence, **base)
    with pytest.raises(ValueError):
        GeographicScope("none", "row_ng_0001", "local", 0.5)
    with pytest.raises(ValueError):
        GeographicScope("content_marker", "", "local", 0.5)
    with pytest.raises(ValueError):
        GeographicScope("content_marker", None, "foreign", 0.5)
    with pytest.raises(ValueError):
        GeographicScope("elsewhere", "row_ng_0001", "local", 0.5)
    with pytest.raises(ValueError):
        GeographicScope("content_marker", "row_ng_0001", "regional", 0.5)
    # A confidence a caller picked for itself is refused: the confidence of a resolved
    # record is the deciding method's own tier and nothing else. The coercion of an
    # integral confidence to a float still happens, and is checked where it lives.
    with pytest.raises(ValueError):
        GeographicScope("content_marker", "row_ng_0001", "contextual", 0.5)
    assert _confidence(1) == 1.0
    assert isinstance(_confidence(1), float)
    # And on the public constructor, which is what the tier rule cost this assertion: the
    # old version built a record at an integral confidence of 1 and read 1.0 back off it,
    # and no method's tier is integral, so that construction can no longer succeed as
    # written. The coverage is restored rather than left on the private helper. An integral
    # confidence still reaches the tier comparison as a number, not refused as one, which
    # is what distinguishes it from a bool.
    with pytest.raises(ValueError, match="deciding method's own tier"):
        GeographicScope("content_marker", "row_ng_0001", "contextual", 1)
    with pytest.raises(ValueError, match="confidence must be a number"):
        GeographicScope("content_marker", "row_ng_0001", "contextual", True)
    # And it is coerced to a float on the way through, which is only observable through the
    # constructor when a method's tier is integral, so one is made integral here.
    monkeypatch.setitem(METHOD_CONFIDENCE, "content_marker", 1.0)
    integral = GeographicScope("content_marker", "row_ng_0001", "contextual", 1)
    assert integral.confidence == 1.0
    assert isinstance(integral.confidence, float)
    monkeypatch.undo()
    # The tier is pinned as a literal rather than read out of METHOD_CONFIDENCE, which is
    # the table this assertion is about: read from it, the assertion held whatever the
    # table said and proved only that the type copies it.
    assert METHOD_CONFIDENCE["content_marker"] == 0.4
    record = GeographicScope("content_marker", " row_ng_0001 ", "contextual", 0.4)
    assert record.evidence_ref == "row_ng_0001"
    assert record.confidence == 0.4
    assert isinstance(record.confidence, float)


def test_a_method_cannot_carry_a_scope_it_cannot_decide_or_a_tier_it_does_not_own():
    # The kernel's claim held of the resolver and not of the type. A record naming the
    # content marker with a local scope at 0.4 was built by hand and accepted, the composer
    # read 0.4 off it as in market confidence, and the batch validator took it for a
    # resolved record, so the whole separation of local from not local rested on nobody
    # constructing one. The table below is read off the resolver by exercising it, so it
    # cannot drift from what resolve_geographic_scope actually produces.
    decided: dict[str, set[str]] = {}
    shapes = itertools.product(
        ("ng", "za"),
        (None, "Nigerian street style", "Sapa Vietnam tour tour tour", CYRILLIC),
        (None, "ng", "za", "gh"),
        (None, "vendor_r1"),
        (None, "ng", "za", "gh"),
        (None, NG_METHOD),
        (None, NG_RECEIPT),
        (None, "economy_sapa_hustle", "music"),
        (None, "ng", "za"),
        (None, "frame_r1"),
    )
    for (
        market,
        title,
        vendor,
        vendor_receipt,
        retained,
        method_id,
        receipt,
        topic,
        frame,
        frame_receipt,
    ) in shapes:
        try:
            resolved = resolve_geographic_scope(
                market=market,
                row_id="row_ng_0001",
                title=title,
                topic_group=topic,
                vendor_market=vendor,
                vendor_receipt_id=vendor_receipt,
                retained_geo_market=retained,
                geo_method_id=method_id,
                geo_receipt_id=receipt,
                search_frame_market=frame,
                search_frame_receipt_id=frame_receipt,
            )
        except ValueError:
            continue
        if resolved.scope == "unknown":
            continue
        decided.setdefault(resolved.method, set()).add(resolved.scope)
        # Every record the resolver builds satisfies the type's own rules, so the
        # constraint forbids nothing the kernel can reach.
        assert resolved.scope in METHOD_SCOPES[resolved.method]
        assert resolved.confidence == METHOD_CONFIDENCE[resolved.method]
        # And a row citing method cites the row itself, never a receipt id.
        assert (resolved.method in ROW_CITING_METHODS) is (resolved.evidence_ref == "row_ng_0001")
    assert {method: sorted(scopes) for method, scopes in sorted(decided.items())} == {
        "blocklist_match": ["foreign"],
        "content_marker": ["contextual"],
        "retained_geo_receipt": ["foreign", "local"],
        "script_marker": ["foreign"],
        "search_frame": ["contextual"],
        "vendor_region": ["foreign", "local"],
    }
    assert {method: sorted(scopes) for method, scopes in METHOD_SCOPES.items()} == {
        method: sorted(scopes) for method, scopes in decided.items()
    }
    assert set(METHOD_SCOPES) == set(METHOD_CONFIDENCE) == METHODS - {"none"}
    assert {"blocklist_match", "script_marker", "content_marker"} == ROW_CITING_METHODS
    # Only the two methods that consult _origin_scope can carry a local scope, so the
    # content marker naming one is refused by the type rather than by a convention.
    assert {method for method, scopes in METHOD_SCOPES.items() if "local" in scopes} == {
        "retained_geo_receipt",
        "vendor_region",
    }
    for method, scopes in METHOD_SCOPES.items():
        for scope in SCOPES - {"unknown"}:
            reference = "row_ng_0001" if method in ROW_CITING_METHODS else NG_RECEIPT
            if scope in scopes:
                assert GeographicScope(method, reference, scope, METHOD_CONFIDENCE[method])
                continue
            with pytest.raises(ValueError, match="cannot decide that scope"):
                GeographicScope(method, reference, scope, METHOD_CONFIDENCE[method])
    with pytest.raises(ValueError, match="cannot decide that scope"):
        GeographicScope("content_marker", "row_ng_0001", "local", 0.4)
    for tier in (0.0, 0.4, 0.5, 0.95, 1.0):
        if tier == METHOD_CONFIDENCE["retained_geo_receipt"]:
            continue
        with pytest.raises(ValueError, match="deciding method's own tier"):
            GeographicScope("retained_geo_receipt", NG_RECEIPT, "local", tier)


def test_the_kernel_text_guard_bounds_normalises_and_refuses_deceptive_characters():
    # The helper bounded nothing: a four kilobyte value, a control character and every
    # invisible passed it, and every field the kernel carries goes through it.
    assert bounded_text("  row_ng_0001  ", "refused") == "row_ng_0001"
    assert TEXT_LIMIT == 256
    assert TEXT_BYTE_LIMIT == 512
    assert bounded_text("r" * TEXT_LIMIT, "refused") == "r" * TEXT_LIMIT
    # One spelling only: a compatibility form normalises onto the plain one rather than
    # standing beside it as a second value.
    assert bounded_text("\uff52\uff4f\uff57_1", "refused") == "row_1"
    for value in (
        None,
        b"row_ng_0001",
        "",
        "   ",
        "\t\n ",
        "r" * (TEXT_LIMIT + 1),
        # 256 characters and 1024 bytes: a character bound alone lets this through.
        "\U0001f600" * 256,
        # invisibles and formatting, each naming nobody on its own
        "\u200b",
        "\ufeff",
        "\u2060",
        "\u00ad",
        "row\u200bng",
        # a bidirectional override
        "principal:\u202edaily",
        # line breaks in a field documented as a single line
        "row\nng",
        "row\rng",
        "row\u2028ng",
        "row\u2029ng",
        # a control character and a lone surrogate that only encodes with surrogatepass
        "row\x00ng",
        "row\x07ng",
        "row\ud800ng",
    ):
        with pytest.raises(ValueError, match=r"\Arefused\Z"):
            bounded_text(value, "refused")
    # Where the value goes on to build an object name it may not walk out of its prefix.
    assert bounded_name("row_ng_0001", "refused") == "row_ng_0001"
    assert bounded_name("days/2026-09-12", "refused") == "days/2026-09-12"
    for value in ("../../etc/passwd", "..", ".", "a/../b", "a/..", "/absolute", "\\windows"):
        with pytest.raises(ValueError, match=r"\Arefused\Z"):
            bounded_name(value, "refused")


def test_evidence_reference_refuses_host_day_and_bare_forms():
    refused = (
        # host names, dotted and bare, and an address
        "punchng.com",
        "www.punchng.com.ng",
        "localhost",
        "192.168.0.1",
        # days in the common spellings, alone, stamped or labelled
        "2026-09-12",
        "2026-09-12T00:30:00Z",
        "12-09-2026",
        "12.09.2026",
        "20260912",
        "day_2026-09-12",
        "2026-09-12 receipt",
        # locations, paths, and empty or non string values
        "https://punchng.example/metro/lagos",
        "punchng.com/metro",
        "",
        "   ",
        None,
        42,
        b"row_ng_0001",
    )
    for value in refused:
        with pytest.raises(ValueError, match="evidence reference"):
            GeographicScope("retained_geo_receipt", value, "local", 0.9)
    for value in (
        "geo_receipt_ng_000123",
        "row_ng_0001",
        "source_run_ng_2026_09_12",
        "ev_9f2c1a7b",
        "row_collected_first",
    ):
        assert GeographicScope("retained_geo_receipt", value, "local", 0.9).evidence_ref == value


def test_retained_geo_receipt_outranks_a_script_marker():
    # Methods decide in descending tier order. A cited receipt at 0.9 beats the
    # script marker at 0.7, the script marker beats a cited vendor frame at 0.6
    # (foreign content under the frame is foreign), and a listed topic collision
    # at 0.95 still beats the receipt.
    tiers = [
        METHOD_CONFIDENCE[method]
        for method in (
            "blocklist_match",
            "retained_geo_receipt",
            "script_marker",
            "vendor_region",
            "content_marker",
        )
    ]
    assert tiers == sorted(tiers, reverse=True)
    receipt = _resolve(title=CYRILLIC, **_receipt())
    assert receipt == GeographicScope("retained_geo_receipt", NG_RECEIPT, "local", 0.9)
    frame = _resolve(
        title=CYRILLIC, vendor_market="ng", vendor_receipt_id="source_run_ng_2026_09_12"
    )
    assert frame.scope == "foreign"
    assert frame == GeographicScope("script_marker", "row_ng_0001", "foreign", 0.7)
    assert _resolve(title=CYRILLIC) == GeographicScope(
        "script_marker", "row_ng_0001", "foreign", 0.7
    )
    blocked = _resolve(
        title="Sapa hotel deals and the best Sapa trek this season",
        topic_group="economy_sapa_hustle",
        **_receipt(),
    )
    assert blocked == GeographicScope("blocklist_match", "row_ng_0001", "foreign", 0.95)
    # A foreign script cancels the subject reading at the same tier: Cyrillic text
    # naming Nigeria is foreign content, not context, and a foreign receipt on it
    # stays foreign at the receipt's tier.
    named = _resolve(title=CYRILLIC + " Nigeria")
    assert named == GeographicScope("script_marker", "row_ng_0001", "foreign", 0.7)
    abroad = _resolve(
        title=CYRILLIC + " Nigeria", **_receipt(market="gb", receipt="geo_receipt_gb_004410")
    )
    assert abroad == GeographicScope(
        "retained_geo_receipt", "geo_receipt_gb_004410", "foreign", 0.9
    )


def test_dense_foreign_latin_text_under_the_frame_on_an_unlisted_topic_is_foreign():
    assert has_foreign_latin_density(TURKISH) == "turkish"
    record = _resolve(
        title=TURKISH,
        topic_group="politics_tinubu",
        vendor_market="ng",
        vendor_receipt_id="source_run_ng_2026_09_12",
    )
    assert record.scope == "foreign"
    assert record == GeographicScope("script_marker", "row_ng_0001", "foreign", 0.7)
    # The same text with no frame at all is foreign on the same evidence.
    assert _resolve(title=TURKISH) == record


# Retained fixtures


def test_nigerian_place_name_collision_resolves_local_only_with_nigerian_receipt():
    # Lagos names a Nigerian state and a town in Portugal. The name alone is no
    # evidence of where the behaviour happened, so it resolves nothing.
    assert _resolve() == GeographicScope("none", None, "unknown", None)
    local = _resolve(**_receipt())
    assert local == GeographicScope("retained_geo_receipt", NG_RECEIPT, "local", 0.9)
    assert local.confidence == METHOD_CONFIDENCE["retained_geo_receipt"]
    # The same collision resolved by a Portuguese receipt is foreign, cited to that
    # receipt, and the Nigerian reading of the name never leaks in.
    portugal = _resolve(title=LAGOS_PT, **_receipt(market="pt", receipt="geo_receipt_pt_000009"))
    assert portugal == GeographicScope(
        "retained_geo_receipt", "geo_receipt_pt_000009", "foreign", 0.9
    )
    # A retained geography without its receipt id cannot be cited, so it resolves
    # nothing, the same rule resolve_wave1_market already applies.
    assert _resolve(**_receipt(receipt=None)) == GeographicScope("none", None, "unknown", None)
    assert _resolve(**_receipt(method=None)) == GeographicScope("none", None, "unknown", None)


def test_known_topic_collision_is_foreign_even_under_the_nigerian_vendor_frame():
    # The Sapa lesson: collected under the NG frame, reads as Vietnamese tourism.
    record = _resolve(
        title="Sapa hotel deals and the best Sapa trek this season",
        url="https://vietnamexpress.example/sapa-hotel",
        topic_group="economy_sapa_hustle",
        vendor_market="ng",
        vendor_receipt_id="source_run_ng_2026_09_12",
    )
    assert record == GeographicScope("blocklist_match", "row_ng_0001", "foreign", 0.95)
    # A script the markets do not write is foreign content, cited to the row.
    assert _resolve(title=CYRILLIC, topic_group=None) == GeographicScope(
        "script_marker", "row_ng_0001", "foreign", 0.7
    )
    # Pure "sapa" is legitimate Nigerian usage and is not blocked.
    assert (
        _resolve(
            title="sapa don hold me this month o", topic_group="economy_sapa_hustle", **_receipt()
        ).scope
        == "local"
    )


def test_international_news_about_a_local_subject_is_contextual_not_local():
    record = _resolve(
        title="Nigeria's central bank holds rates as inflation cools",
        text=WIRE_STORY,
        url="https://www.reuters.example/world/africa/nigeria-rates",
        **_receipt(market="gb", receipt="geo_receipt_gb_004410"),
    )
    assert record == GeographicScope("content_marker", "row_ng_0001", "contextual", 0.4)
    assert record.scope != "local"
    assert local_observation_refs([record]) == ()
    # Without the subject reference the same foreign origin is plain foreign.
    foreign = _resolve(
        title="Bank of England holds rates as inflation cools",
        text=WIRE_STORY,
        **_receipt(market="gb", receipt="geo_receipt_gb_004410"),
    )
    assert foreign == GeographicScope(
        "retained_geo_receipt", "geo_receipt_gb_004410", "foreign", 0.9
    )


def test_creator_registration_country_is_not_audience_location():
    unknown = GeographicScope("none", None, "unknown", None)
    assert _resolve(creator_registration_market="ng") == unknown
    # Registered in Nigeria, behaviour retained in South Africa: not local to NG.
    abroad = _resolve(
        creator_registration_market="ng",
        **_receipt(market="za", receipt="geo_receipt_za_000777"),
    )
    assert abroad == GeographicScope(
        "retained_geo_receipt", "geo_receipt_za_000777", "foreign", 0.9
    )
    # Registered abroad, behaviour retained in Nigeria: local, cited to the receipt
    # that carries the behaviour, not to the registration.
    home = _resolve(creator_registration_market="gb", **_receipt())
    assert home == GeographicScope("retained_geo_receipt", NG_RECEIPT, "local", 0.9)
    with pytest.raises(ValueError):
        _resolve(creator_registration_market="Nigeria")


def test_vendor_region_resolves_only_through_its_own_receipt():
    assert _resolve(vendor_market="ng") == GeographicScope("none", None, "unknown", None)
    local = _resolve(vendor_market="ng", vendor_receipt_id="source_run_ng_2026_09_12")
    assert local == GeographicScope("vendor_region", "source_run_ng_2026_09_12", "local", 0.6)
    assert local.confidence < METHOD_CONFIDENCE["retained_geo_receipt"]
    foreign = _resolve(
        title=LAGOS_PT, vendor_market="pt", vendor_receipt_id="source_run_pt_2026_09_12"
    )
    assert foreign == GeographicScope("vendor_region", "source_run_pt_2026_09_12", "foreign", 0.6)
    # Vendor and retained geography disagree: the existing rule refuses, loudly.
    with pytest.raises(ValueError, match="vendor and retained geography disagree"):
        _resolve(vendor_market="za", vendor_receipt_id="source_run_za_1", **_receipt())
    with pytest.raises(ValueError, match="vendor and retained geography disagree"):
        _resolve(
            vendor_market="ng",
            vendor_receipt_id="source_run_ng_1",
            **_receipt(market="gb", receipt="geo_receipt_gb_1"),
        )


def test_observation_without_a_date_keeps_unknown_and_is_not_foreign():
    # The kernel never reads a date, so an undated observation is judged on its
    # geographic evidence alone: nothing cited, unknown preserved.
    record = _resolve(url="https://punchng.example/metro/lagos-traffic")
    assert record.scope == "unknown"
    assert record.confidence is None
    assert record.scope != "foreign"
    assert _resolve(**_receipt()).scope == "local"
    with pytest.raises(TypeError):
        _resolve(published_at=None)


def test_copied_wire_story_on_two_hosts_is_one_observation():
    wire = _receipt(receipt="geo_receipt_nan_wire_7781", method="geo_method_wire_origin_v1")
    punch = _resolve(
        row_id="row_ng_punch_11",
        title=WIRE_STORY,
        url="https://punchng.example/business/cbn-holds-rate",
        **wire,
    )
    vanguard = _resolve(
        row_id="row_ng_vanguard_12",
        title=WIRE_STORY,
        url="https://vanguardngr.example/2026/09/cbn-holds-rate",
        **wire,
    )
    assert punch == vanguard
    assert punch.scope == "local"
    assert punch.evidence_ref == "geo_receipt_nan_wire_7781"
    assert local_observation_refs([punch, vanguard]) == ("geo_receipt_nan_wire_7781",)


def test_separate_people_repeating_similar_language_are_independent():
    first = _resolve(
        row_id="row_ng_tiktok_31",
        title="sapa don hold me this month o, na wire I dey eat",
        **_receipt(receipt="geo_receipt_ng_a1"),
    )
    second = _resolve(
        row_id="row_ng_tiktok_32",
        title="sapa hold me this month, na wire we dey chop",
        **_receipt(receipt="geo_receipt_ng_b2"),
    )
    assert first.scope == second.scope == "local"
    assert first.evidence_ref != second.evidence_ref
    assert local_observation_refs([first, second, first]) == (
        "geo_receipt_ng_a1",
        "geo_receipt_ng_b2",
    )


# Counterexamples: each names a policy that must fail


def test_policy_earliest_collection_as_proof_of_originator_fails():
    # The kernel has no collection time input, so a collector cannot become the
    # originator by being first.
    with pytest.raises(TypeError):
        _resolve(collected_at="2026-09-12T00:30:00Z")
    wire = _receipt(receipt="geo_receipt_nan_wire_7781")
    early = _resolve(row_id="row_collected_first", title=WIRE_STORY, **wire)
    late = _resolve(row_id="row_collected_later", title=WIRE_STORY, **wire)
    assert early == late
    assert "row_collected_first" not in local_observation_refs([early, late])


def test_policy_host_or_day_as_identity_fails():
    for identity in ("punchng.com", "www.punchng.com.ng", "2026-09-12", "2026-09-12T00:30:00Z"):
        with pytest.raises(ValueError, match="evidence reference"):
            GeographicScope("retained_geo_receipt", identity, "local", 0.9)
    with pytest.raises(ValueError, match="evidence reference"):
        GeographicScope("content_marker", "https://punchng.example/metro/lagos", "contextual", 0.4)
    # A Nigerian host is not Nigerian behaviour.
    hosted = _resolve(url="https://punchng.example.com.ng/metro/lagos-traffic")
    assert hosted == GeographicScope("none", None, "unknown", None)
    cited = _resolve(url="https://punchng.example.com.ng/metro/lagos-traffic", **_receipt())
    assert "punchng" not in cited.evidence_ref
    assert "2026" not in cited.evidence_ref


def test_policy_different_hosts_as_automatic_independence_fails():
    hosts = (
        "https://punchng.example/metro/lagos",
        "https://vanguardngr.example/metro/lagos",
        "https://thenationonlineng.example/metro/lagos",
    )
    unresolved = [_resolve(row_id=f"row_{index}", url=url) for index, url in enumerate(hosts)]
    assert {record.scope for record in unresolved} == {"unknown"}
    assert local_observation_refs(unresolved) == ()
    wire = _receipt(receipt="geo_receipt_nan_wire_7781")
    copies = [_resolve(row_id=f"row_{index}", url=url, **wire) for index, url in enumerate(hosts)]
    assert len(local_observation_refs(copies)) == 1


def test_policy_global_story_as_local_behaviour_fails():
    global_story = _resolve(
        title="Global markets: Nigeria, Kenya and South Africa face a stronger dollar",
        url="https://www.ft.example/content/global-markets",
        **_receipt(market="us", receipt="geo_receipt_us_000001"),
    )
    assert global_story.scope == "contextual"
    assert local_observation_refs([global_story]) == ()
    # Naming the country in the content is context, never local behaviour.
    named = _resolve(title="Nigeria is the loudest room in African football tonight")
    assert named == GeographicScope("content_marker", "row_ng_0001", "contextual", 0.4)
    assert named.scope != "local"
    # A city name alone is not even context: it collides.
    assert _resolve(title=LAGOS_PT).scope == "unknown"


def test_local_observation_refs_rejects_foreign_records():
    with pytest.raises(TypeError):
        local_observation_refs([{"scope": "local", "evidence_ref": "x"}])
    assert local_observation_refs([]) == ()
