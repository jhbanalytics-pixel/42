"""The K5 union of the Ask exclusions (a disclosed ad, a copy under another handle) with the corroborated grouping
over the answer's pool (checks.py _evidence_label). The reason counts unrelated author groups, as the label does."""
import copy
import types

from core.agent import checks


def rec(rid, platform, handle, text, flags=None):
    return {"id": rid, "platform": platform, "handle": handle,
            "url": f"https://example.test/{platform}/{handle.strip('@')}/{rid}",
            "posted_at": "2026-09-27T14:05:00+02:00", "market": "ZA", "text": text,
            "engagement": {}, "flags": flags or []}


T = "Every taxi rank in Joburg is playing this amapiano track today, nobody can stop it"
U1 = "These trainers are everywhere at taxi ranks this week"
U2 = "My cousin in Durban queued two hours for these trainers today"
U3 = "My uncle in Durban asked me for that new song yesterday"


def label(records, pool=None, numbers=False):
    claim = {"id": "c1", "text": "Posts mention the new trainers.", "kind": "observation",
             "numbers": [1] if numbers else []}
    ctx = types.SimpleNamespace(evidence={r["id"]: r for r in pool}) if pool is not None else None
    return checks._max_label(claim, records, (), ctx)


def test_k5_a_disclosed_ad_inside_an_otherwise_unrelated_pair_on_two_platforms_is_single_source():
    records = [rec("p_a", "tiktok", "@fan", U1), rec("p_b", "x", "@influencer", "Obsessed with my new trainers this season #ad")]
    assert label(records) == ("single_source", "1 unrelated author group on 1 platform")


def test_k5_a_disclosed_ad_beside_two_unrelated_organic_authors_on_two_platforms_still_corroborates():
    records = [rec("p_a", "tiktok", "@fan", U1), rec("p_b", "x", "@influencer", "Obsessed with my new trainers this season #ad"),
               rec("p_c", "instagram", "@cousin", U2)]
    assert label(records) == ("corroborated", "2 unrelated author groups on 2 platforms")


def test_k5_a_copied_post_across_x_and_twitter_is_single_source():
    records = [rec("p_a", "x", "@creator_a", T), rec("p_b", "twitter", "@creator_b", T)]
    assert label(records) == ("single_source", "1 unrelated author group on 1 platform")


def test_k5_a_pool_with_only_paid_authors_is_single_source():
    records = [rec("p_a", "tiktok", "@a", U1 + " #ad"), rec("p_b", "x", "@b", U2 + " #sponsored"),
               rec("p_c", "instagram", "@c", U3 + " paid partnership with the brand")]
    assert label(records) == ("single_source", "0 unrelated author groups on 0 platforms")
    assert label(records, pool=records) == ("single_source", "0 unrelated author groups on 0 platforms")


def test_k5_a_copy_under_another_handle_lends_no_platform_to_corroboration():
    # tiktok original, its copy on x, and an unrelated tiktok author: the copy is not an author, so one platform.
    records = [rec("p_a", "tiktok", "@creator_a", T), rec("p_b", "x", "@creator_b", T), rec("p_c", "tiktok", "@creator_c", U3)]
    assert label(records) == ("observed", "2 unrelated author groups on 1 platform")


def test_k5_a_copy_of_a_disclosed_ad_lends_no_platform_either():
    records = [rec("p_a", "tiktok", "@creator_a", T + " #ad"), rec("p_b", "x", "@creator_b", T),
               rec("p_c", "instagram", "@creator_c", U3)]
    assert label(records) == ("single_source", "1 unrelated author group on 1 platform")


def test_k5_the_label_never_exceeds_what_its_own_reason_counts():
    # Corroborated needs at least 2 groups on 2 platforms or 3 groups; the reason string must agree with the label.
    import itertools
    import re
    texts = [U1, U2, U3, T, T, T + " #ad", U1 + " #sponsored"]
    plats = ["tiktok", "x", "twitter", "instagram"]
    for n in (2, 3):
        for combo in itertools.product(range(len(texts)), repeat=n):
            for pl in itertools.product(plats, repeat=n):
                records = [rec(f"p_{i}", pl[i], f"@h{i}", texts[combo[i]]) for i in range(n)]
                top, why = label(records)
                authors, platforms = map(int, re.findall(r"\d+", why)[:2])
                if top == "corroborated":
                    assert (authors >= 2 and platforms >= 2) or authors >= 3, (records, top, why)


def test_k5_a_disclosed_ad_in_the_pool_still_links_the_authors_it_mentions():
    cited = [rec("p_a", "tiktok", "@fan", U1), rec("p_b", "instagram", "@cousin", U2)]
    paid = rec("p_p", "x", "@brand", "Thanks @fan and @cousin for showing our trainers #ad")
    assert label(cited, pool=cited + [paid])[0] == "observed"
    quiet = rec("p_p", "x", "@brand", "Our trainers are in every store this week #ad")
    assert label(cited, pool=cited + [quiet])[0] == "corroborated"


def test_k5_union_reads_without_changing_the_records():
    records = [rec("p_a", "tiktok", "@creator_a", T + " #ad"), rec("p_b", "x", "@creator_b", T)]
    before = copy.deepcopy(records)
    label(records, pool=records)
    assert records == before
