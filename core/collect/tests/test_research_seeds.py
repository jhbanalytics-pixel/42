from core.collect import curated_creators, research_seeds


def research_markdown():
    return """# Local research

## South Africa (ZA)

### Creator profiles

| Platform | Handle | Profile URL | Notes |
|---|---|---|---|
| Instagram | alpha | [profile|link](https://www.instagram.com/alpha/?hl=en) | confirmed |
| TikTok | beta | https://www.tiktok.com/@beta | UNCONFIRMED because activity, followers and residency need checking |
| Instagram | shared | https://www.instagram.com/shared/?hl=en | confirmed |
| Instagram | shared | https://instagram.com/shared/ | confirmed |

### News, culture and entertainment accounts

| Platform | Handle | Social URL | Notes |
|---|---|---|---|
| Instagram | desk | https://www.instagram.com/desk/ | confirmed |

### Dated moments

| Date | Moment | Notes |
|---|---|---|
| 15 March | Sample event | confirmed |
| 27 April | Held event | UNCONFIRMED in a row note |

## Calendar reference corrections

### Dated moments

| Market | Date | Moment |
|---|---|---|
| KE | 19 May | Audit-only event |

## Nigeria (NG)

### Creator profiles

| Platform | Handle | Profile URL | Notes |
|---|---|---|---|
| TikTok | gamma | https://www.tiktok.com/@gamma | confirmed |
"""


def test_parser_uses_market_sections_resets_appendix_and_classifies_renamed_outlets():
    parsed = research_seeds.parse_research_markdown(research_markdown())

    assert [(candidate["record"]["handle"], candidate["type"])
            for candidate in parsed["profiles"]] == [("alpha", "creator"), ("desk", "outlet"), ("gamma", "creator")]
    assert [(moment["market"], moment["date_format"], moment["reason"])
            for moment in parsed["moments"]] == [
                ("ZA", "day_month_name", "seed_comparison_deferred")]
    assert all("Audit-only event" not in str(value) for value in parsed.values())


def test_parser_skips_every_unconfirmed_row_and_returns_only_allowed_profile_fields():
    parsed = research_seeds.parse_research_markdown(research_markdown())

    assert parsed["skipped"] == [
        {"market": "ZA", "type": "creator", "line": 10, "reason": "unconfirmed"},
        {"market": "ZA", "type": "creator", "line": 11, "reason": "duplicate_canonical_url"},
        {"market": "ZA", "type": "creator", "line": 12, "reason": "duplicate_canonical_url"},
        {"market": "ZA", "type": "moment", "line": 25, "reason": "unconfirmed"},
    ]
    assert all(set(candidate["record"]) == {
        "platform", "handle", "url", "market", "source", "active"
    } for candidate in parsed["profiles"])
    assert all(candidate["record"]["source"] == "research_confirmed" for candidate in parsed["profiles"])
    assert all(candidate["record"]["active"] is True for candidate in parsed["profiles"])
    assert all(set(skip) == {"market", "type", "line", "reason"} for skip in parsed["skipped"])


def test_canonical_duplicate_urls_skip_both_profile_rows():
    parsed = research_seeds.parse_research_markdown(research_markdown())

    assert all(candidate["record"]["handle"] != "shared" for candidate in parsed["profiles"])
    assert [item for item in parsed["skipped"] if item["reason"] == "duplicate_canonical_url"] == [
        {"market": "ZA", "type": "creator", "line": 11, "reason": "duplicate_canonical_url"},
        {"market": "ZA", "type": "creator", "line": 12, "reason": "duplicate_canonical_url"},
    ]


def test_merge_prefers_research_tag_and_retains_other_known_market_feeds():
    parsed = research_seeds.parse_research_markdown(research_markdown())
    existing = [
        {"platform": "instagram", "handle": "alpha", "url": "https://www.instagram.com/alpha/",
         "market": "ZA", "source": "ogilvy_uefa_list", "active": True},
        {"platform": "tiktok", "handle": "gamma", "url": "https://www.tiktok.com/@gamma",
         "market": "KE", "source": "ogilvy_uefa_list", "active": True},
    ]

    merged = research_seeds.merge_profiles(existing, parsed["profiles"])

    assert [(row["handle"], row["market"], row["source"], row["active"])
            for row in merged["records"]] == [
                ("alpha", "ZA", "research_confirmed", True),
                ("gamma", "KE", "ogilvy_uefa_list", True),
                ("desk", "ZA", "research_confirmed", True),
            ]
    assert [(item["type"], item["status"]) for item in merged["statuses"]] == [
        ("creator", "already_present"), ("outlet", "new")]
    assert merged["skipped"] == [
        {"market": "NG", "type": "creator", "line": 41, "reason": "ambiguous_cross_market"}]


def test_merge_flags_cross_market_identity_without_guessing_and_keeps_history():
    parsed = research_seeds.parse_research_markdown(research_markdown())
    existing = [{"platform": "instagram", "handle": "alpha", "url": "https://www.instagram.com/alpha/",
                 "market": "ZA", "source": "ogilvy_uefa_list", "active": False}]
    candidate = dict(parsed["profiles"][0], record={**parsed["profiles"][0]["record"], "market": "KE"})

    merged = research_seeds.merge_profiles(existing, [candidate])

    assert merged["records"] == existing
    assert merged["statuses"] == []
    assert merged["skipped"] == [
        {"market": "KE", "type": "creator", "line": 9, "reason": "ambiguous_cross_market"}]


def test_merge_keeps_duplicate_existing_rows_inactive_instead_of_deleting_them():
    existing = [
        {"platform": "instagram", "handle": "same", "url": "https://www.instagram.com/same/?hl=en",
         "market": "ZA"},
        {"platform": "instagram", "handle": "same", "url": "https://instagram.com/same/",
         "market": "NG"},
    ]

    merged = research_seeds.merge_profiles(existing, [])

    assert len(merged["records"]) == 2
    assert [row["active"] for row in merged["records"]] == [False, False]
    assert all(row["source"] == "ogilvy_uefa_list" for row in merged["records"])
    assert merged["skipped"] == []


def test_preview_report_contains_counts_and_no_raw_row_text():
    parsed = research_seeds.parse_research_markdown(research_markdown())
    merged = research_seeds.merge_profiles([], parsed["profiles"])

    report = research_seeds.build_preview_report(parsed, merged)

    assert report["counts"]["ZA"]["creator"] == {
        "new": 1, "already_present": 0, "skipped_unconfirmed": 1, "skipped_other": 2
    }
    assert report["counts"]["NG"]["creator"]["new"] == 1
    assert report["counts"]["ZA"]["outlet"]["new"] == 1
    assert report["counts"]["ZA"]["moment"]["seed_comparison_deferred"] == 1
    assert all(set(record) == {"platform", "handle", "url", "market", "source", "active"}
               for record in report["profiles"])
    report_text = str(report)
    for value in ("UNCONFIRMED", "activity, followers", "residency", "Audit-only event", "Sample event"):
        assert value not in report_text


def test_audited_account_csv_keeps_only_profile_fields_and_source_tag():
    source = """Market,Handle / page,Category,Platform,Decision,Rationale,Owner name/role,Evidence
South Africa,@alpha,Creator,Instagram,Keep,private rationale,private owner,https://www.instagram.com/alpha/?hl=en
Kenya,@x_handle,Outlet,X,Keep,private rationale,private owner,https://x.com/x_handle
"""

    parsed = research_seeds.parse_audited_accounts_csv(source)

    assert parsed["records"] == [
        {"handle": "alpha", "platform": "instagram", "url": "https://www.instagram.com/alpha/",
         "market": "ZA", "source_tag": "ogilvy_audit_2026-09-30"},
        {"handle": "x_handle", "platform": "twitter", "url": "https://x.com/x_handle",
         "market": "KE", "source_tag": "ogilvy_audit_2026-09-30"},
    ]
    assert all(set(record) == set(research_seeds.AUDITED_ACCOUNT_FIELDS) for record in parsed["records"])
    assert "private rationale" not in str(parsed)
    assert "private owner" not in str(parsed)


def test_dated_topics_csv_normalizes_ranges_and_drops_unapproved_columns():
    source = """date,country,topic,event,venue,Rationale,Owner name/role
2026-10-03 to 2026-10-11,South Africa,Sport,Sample event,Johannesburg,private rationale,private owner
2026-10-04,Nigeria,Culture,Second event,Lagos,private rationale,private owner
"""

    parsed = research_seeds.parse_dated_topics_csv(source, research_seeds.THABANG_CONFIRMED_TAG)

    assert parsed["records"] == [
        {"start_date": "2026-10-03", "end_date": "2026-10-11", "market": "ZA", "topic": "Sport",
         "event": "Sample event", "venue": "Johannesburg", "source_tag": "ogilvy_thabang_confirmed"},
        {"start_date": "2026-10-04", "end_date": "2026-10-04", "market": "NG", "topic": "Culture",
         "event": "Second event", "venue": "Lagos", "source_tag": "ogilvy_thabang_confirmed"},
    ]
    assert all(set(record) == set(research_seeds.DATED_TOPIC_FIELDS) for record in parsed["records"])
    assert "private rationale" not in str(parsed)
    assert "private owner" not in str(parsed)


def test_dated_topics_csv_accepts_thapelo_confirmed_provenance():
    source = """date,country,topic,event,venue
2026-10-03,South Africa,Music,Sample event,Cape Town
"""

    parsed = research_seeds.parse_dated_topics_csv(source, "ogilvy_thapelo_confirmed")

    assert parsed["records"][0]["source_tag"] == "ogilvy_thapelo_confirmed"


def test_audited_profiles_convert_to_the_existing_loader_contract():
    records = [
        {"handle": "alpha", "platform": "instagram", "url": "https://www.instagram.com/alpha/",
         "market": "ZA", "source_tag": "ogilvy_audit_2026-09-30"},
        {"handle": "news_x", "platform": "twitter", "url": "https://x.com/news_x",
         "market": "NG", "source_tag": "ogilvy_audit_2026-09-30"},
    ]

    converted = research_seeds.audited_accounts_for_curated_manifest(records)

    assert converted == [
        {"platform": "instagram", "handle": "alpha", "url": "https://www.instagram.com/alpha/",
         "market": "ZA", "source": "ogilvy_audit_2026-09-30", "active": True,
         "source_tags": ["ogilvy_audit_2026-09-30"]},
        {"platform": "twitter", "handle": "news_x", "url": "https://x.com/news_x",
         "market": "NG", "source": "ogilvy_audit_2026-09-30", "active": False,
         "source_tags": ["ogilvy_audit_2026-09-30"]},
    ]


def test_merge_accepts_new_audited_profiles_with_inactive_x(monkeypatch):
    def quote(*_args, **_kwargs):
        return 1

    monkeypatch.setattr(curated_creators, "quote_for", quote)
    profiles = research_seeds.audited_accounts_for_curated_manifest([
        {"handle": "alpha", "platform": "instagram", "url": "https://www.instagram.com/alpha/",
         "market": "ZA", "source_tag": "ogilvy_audit_2026-09-30"},
        {"handle": "news_x", "platform": "twitter", "url": "https://x.com/news_x",
         "market": "NG", "source_tag": "ogilvy_audit_2026-09-30"},
    ])
    candidates = [{"record": row, "type": "creator", "line": index}
                 for index, row in enumerate(profiles, 1)]

    merged = research_seeds.merge_profiles([], candidates)

    assert [(row["platform"], row["market"], row["source"], row["active"])
            for row in merged["accepted"]] == [
                ("instagram", "ZA", "ogilvy_audit_2026-09-30", True),
                ("twitter", "NG", "ogilvy_audit_2026-09-30", False),
            ]
    assert [item["status"] for item in merged["statuses"]] == ["new", "new"]


def test_merge_appends_audit_tag_without_changing_existing_row_or_source(monkeypatch):
    monkeypatch.setattr(curated_creators, "quote_for", lambda *_args, **_kwargs: 1)
    candidate = research_seeds.audited_accounts_for_curated_manifest([
        {"handle": "alpha", "platform": "instagram", "url": "https://www.instagram.com/alpha/",
         "market": "ZA", "source_tag": "ogilvy_audit_2026-09-30"},
    ])[0]
    existing = [{"platform": "instagram", "handle": "alpha", "url": "https://www.instagram.com/alpha/",
                 "market": "ZA", "source": "research_confirmed", "active": False}]

    item = {"record": candidate, "type": "creator", "line": 2}
    merged = research_seeds.merge_profiles(existing, [item])

    assert merged["records"] == [{**existing[0], "source_tags": [
        "research_confirmed", "ogilvy_audit_2026-09-30"
    ]}]
    assert merged["records"][0]["source"] == "research_confirmed"
    assert merged["records"][0]["active"] is False
    assert merged["statuses"] == [{"market": "ZA", "type": "creator", "line": 2, "status": "already_present"}]
    assert merged["skipped"] == []

    repeated = research_seeds.merge_profiles(merged["records"], [item])

    assert repeated["records"] == merged["records"]
    assert repeated["records"][0]["source_tags"].count("ogilvy_audit_2026-09-30") == 1


def test_expanded_topic_venue_conflicts_are_kept_held_in_sidecar():
    base_text = """date,country,topic,event,venue
2026-10-03 to 2026-10-11,South Africa,Sport,Cafe week,Cape Town
"""
    expanded_text = """date,country,topic,event,venue
2026-10-03 to 2026-10-11,South Africa,Sport,Cafe week,Other venue
"""
    base = research_seeds.parse_dated_topics_csv(base_text, research_seeds.THABANG_CONFIRMED_TAG)
    expanded = research_seeds.parse_dated_topics_csv(expanded_text, research_seeds.EXPANDED_UNVERIFIED_TAG)
    base_record = dict(base["records"][0])

    reconciled = research_seeds.reconcile_expanded_topics(base, expanded)

    assert reconciled["new_records"] == []
    assert reconciled["tag_additions"] == []
    assert reconciled["held_variants"] == [reconciled["sidecar"][0]]
    assert reconciled["sidecar"][0]["record"] == expanded["records"][0]
    assert reconciled["sidecar"][0]["classification"] == "base_identity_payload_conflict_held"
    assert base["records"][0] == base_record


def test_expanded_topic_variant_is_preserved_without_holding_the_source_tag():
    base_text = """date,country,topic,event,venue
2026-10-03,Kenya,Culture / History / Impact,Mashujaa Day PUBLIC HOLIDAY,Kenya
"""
    expanded_text = """date,country,topic,event,venue
2026-10-03,Kenya,Culture / History,Mashujaa Day PUBLIC HOLIDAY,Kenya
"""
    base = research_seeds.parse_dated_topics_csv(base_text, research_seeds.THABANG_CONFIRMED_TAG)
    expanded = research_seeds.parse_dated_topics_csv(expanded_text, research_seeds.EXPANDED_UNVERIFIED_TAG)

    reconciled = research_seeds.reconcile_expanded_topics(base, expanded)

    assert reconciled["new_records"] == []
    assert reconciled["tag_additions"] == [{
        **base["records"][0], "source_tag": "ogilvy_expanded_unverified"
    }]
    assert reconciled["held_variants"] == []
    assert reconciled["sidecar"][0]["classification"] == "base_identity_topic_variant_preserved"
    assert reconciled["sidecar"][0]["record"]["topic"] == "Culture / History"
    assert reconciled["sidecar"][0]["held"] is False


def test_expanded_topic_identity_uses_event_instead_of_topic():
    base_text = """date,country,topic,event,venue
2026-10-03,South Africa,Sport,First event,Cape Town
"""
    expanded_text = """date,country,topic,event,venue
2026-10-03,South Africa,Sport,Second event,Cape Town
"""
    base = research_seeds.parse_dated_topics_csv(base_text, research_seeds.THABANG_CONFIRMED_TAG)
    expanded = research_seeds.parse_dated_topics_csv(expanded_text, research_seeds.EXPANDED_UNVERIFIED_TAG)

    reconciled = research_seeds.reconcile_expanded_topics(base, expanded)

    assert reconciled["new_records"] == expanded["records"]
    assert reconciled["tag_additions"] == []
    assert reconciled["held_variants"] == []


def test_expanded_topic_payload_match_uses_only_case_punctuation_and_accent_normalization():
    base_text = """date,country,topic,event,venue
2026-10-03,South Africa,Sport,Café week!,São-Paulo
"""
    expanded_text = """date,country,topic,event,venue
2026-10-03,South Africa,Sport,CAFE WEEK,Sao Paulo
"""
    base = research_seeds.parse_dated_topics_csv(base_text, research_seeds.THABANG_CONFIRMED_TAG)
    expanded = research_seeds.parse_dated_topics_csv(expanded_text, research_seeds.EXPANDED_UNVERIFIED_TAG)

    reconciled = research_seeds.reconcile_expanded_topics(base, expanded)

    assert reconciled["new_records"] == []
    assert reconciled["tag_additions"] == [{
        **base["records"][0], "source_tag": "ogilvy_expanded_unverified"
    }]
    assert reconciled["sidecar"][0]["classification"] == "base_identity_payload_equal"


def test_repeated_expanded_identity_with_conflicting_payloads_keeps_both_variants_held():
    base_text = """date,country,topic,event,venue
2026-10-03,South Africa,Sport,Cafe week,Sao Paulo
"""
    expanded_text = """date,country,topic,event,venue
2026-10-03,South Africa,Sport,Cafe week,Other venue
2026-10-03,South Africa,Music,Cafe week,Another venue
"""
    base = research_seeds.parse_dated_topics_csv(base_text, research_seeds.THABANG_CONFIRMED_TAG)
    expanded = research_seeds.parse_dated_topics_csv(expanded_text, research_seeds.EXPANDED_UNVERIFIED_TAG)

    reconciled = research_seeds.reconcile_expanded_topics(base, expanded)

    assert reconciled["new_records"] == []
    assert reconciled["tag_additions"] == []
    assert len(reconciled["held_variants"]) == 2
    assert [item["record"] for item in reconciled["sidecar"]] == expanded["records"]
    assert {item["classification"] for item in reconciled["held_variants"]} == {
        "repeated_identity_payload_conflict_held"
    }
