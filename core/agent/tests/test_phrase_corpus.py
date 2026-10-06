"""Phrase corpus for the K2 and K6 field checks (TRUST.md section 3, steps 3 and 4).

Each row is an analyst or strategist phrasing, the numbers entries pinned for it, and whether the gate passes or cuts
it. Every row runs as the short answer, as a claim and as a writer gap, and must give the same verdict in all three.
The rows hold every text from the trust gate reviews so far, clean and leaking. Keep it green: a change that fixes one
phrasing and breaks another shows up here.
"""

import copy
from datetime import date, datetime

import pytest

from core.agent.checks import check_answer
from core.agent.context import RunContext

RUN = "r_corpus"
AS_OF = "2026-09-28T06:10:00+02:00"
WINDOW = (date(2026, 9, 21), date(2026, 9, 28))
SQL = "SELECT d.v FROM intelligence_42_core.v_item_daily_current d WHERE d.tag = @tag"


def record(rid, platform, handle, text, url=None):
    return {"id": rid, "platform": platform, "handle": handle,
            "url": url or f"https://example.test/{platform}/{handle.strip('@')}/{rid}",
            "posted_at": "2026-09-27T14:05:00+02:00", "market": "ZA", "text": text, "engagement": {}, "flags": []}


STORED = [
    record("tt_1", "tiktok", "@mpho.cooks.za", "Sunday 7 colours check, beetroot next to the chakalaka #7colours"),
    record("tt_2", "tiktok", "@thandi_eats_jozi", "Pap AND rice on one plate is a crime #7colours"),
    record("tt_3", "tiktok", "@kay.plates", "My 7 colours plate for today, rate it in the comments"),
    record("x_1", "x", "@lerato_mk", "Rate my 7 colours honestly, the butternut is doing the most"),
    record("tt_9", "tiktok", "@dj5000", "Sunday set for the plates #amapiano2026"),
]

# (text, pinned entries, verdict)
CORPUS = [
    # clean analyst phrasing with no figure
    ("Sunday plate ratings rose on TikTok and X this week.", [], "pass"),
    ("Creators in Durban and Soweto rated their Sunday plates.", [], "pass"),
    ("One creator started the rating habit.", [], "pass"),
    ("One of the creators rated every plate.", [], "pass"),
    ("Two platforms carried most of the talk.", [], "pass"),
    ("The talk moved from TikTok to X over the weekend.", [], "pass"),
    ("Plate ratings ran through the last 8 days.", [], "pass"),
    ("Plate ratings rose between 21 and 27 September.", [], "pass"),
    ("Plate ratings peaked on 27 September.", [], "pass"),
    ("Plate ratings have run since 2019.", [], "pass"),
    ("Plate ratings in 2026 look like a weekly ritual.", [], "pass"),
    ("Talk of the 2027 election crept into plate posts.", [], "pass"),
    ("AFCON 2027 jerseys showed up in plate posts.", [], "pass"),
    ("The first half of the day was quiet.", [], "pass"),
    ("Posts rose during the second half of the weekend.", [], "pass"),
    ("The third of the plates was the best rated.", [], "pass"),
    # ids, handles, hashtags, links and routes this run holds
    ("Posts tagged #7colours led the week.", [], "pass"),
    ("Posts under #amapiano2026 joined in.", [], "pass"),
    ("The plate from @dj5000 drew replies.", [], "pass"),
    ("The post at https://example.test/tiktok/kay.plates/tt_3 drew replies.", [], "pass"),
    ("Posts tt_1 and x_1 started it.", [], "pass"),
    ("The query q_1 counted the posts.", [], "pass"),
    ("A reddit/search/v2 pass found nothing new.", [], "pass"),
    # ids, handles, hashtags and links this run does not hold
    ("Plate posts reached @5000 views.", [], "cut"),
    ("Plate posts hit x_5000 views.", [], "cut"),
    ("Plate posts logged views_5000 this week.", [], "cut"),
    ("Plate sales hit R_5000 in sales.", [], "cut"),
    ("Plate posts trended as #300percent.", [], "cut"),
    ("Plate posts were #top10 in ZA.", [], "cut"),
    ("Plate posts at https://stats.example/5000-posts led.", [], "cut"),
    ("Plate posts reached @5k.", [], "cut"),
    ("Posts by @dj5001 led the week.", [], "cut"),
    ("Posts under #amapiano2027 led the week.", [], "cut"),
    ("Posts in tt_99 led the week.", [], "cut"),
    # periods
    ("Plate posts rose in Q3 2026.", [], "pass"),
    ("Plate posts may keep rising into Q1 2028.", [], "pass"),
    ("Plate posts will rise in Q1 2031.", [], "cut"),
    ("Plate posts held up through H1 2026.", [], "pass"),
    ("Plate posts in FY2026 beat the prior year.", [], "pass"),
    ("Plate posts in Q3 of 2026 beat the prior quarter.", [], "pass"),
    ("Plate posts in Q2, 2026 were flat.", [], "pass"),
    ("The 1990s sound is back on Sunday plates.", [], "pass"),
    ("The '90s sound is back on Sunday plates.", [], "pass"),
    ("2000s kwaito is back on Sunday plates.", [], "pass"),
    ("The 1995s sound is back on Sunday plates.", [], "cut"),
    # clock times
    ("Plate posts peaked at 21:00.", [], "pass"),
    ("Plate posts peaked between 19:00 and 21:00.", [], "pass"),
    ("Plate posts rose after 18:00.", [], "pass"),
    ("Plate posts peaked around 21:00.", [], "pass"),
    ("Plate posts have climbed since 18:00.", [], "pass"),
    ("Plate posts spike Sunday 18:00.", [], "pass"),
    ("Plate posts peaked 20:00 to 22:00.", [], "pass"),
    ("Posts peak 19:00-21:00 on Sundays.", [], "pass"),
    ("Plate posts ran on Sunday, 18:00 to 20:00.", [], "pass"),
    ("Plate posts peaked at around 21:00.", [], "pass"),
    ("Plate posts peaked at roughly 18:30.", [], "pass"),
    ("Plate posts peaked at 6pm.", [], "pass"),
    ("Plate posts peaked at 6 p.m.", [], "pass"),
    ("Plate posts peaked at 18h00.", [], "pass"),
    ("Plate posts ran 6:30 pm to 8 pm.", [], "pass"),
    ("Plate posts ran from 18h00 to 20h30.", [], "pass"),
    ("The 21:00 peak carried most plate posts.", [], "pass"),
    ("The 20:00 slot carried most posts.", [], "pass"),
    ("21:00 is the peak hour for plate posts.", [], "pass"),
    ("Plate talk followed the 18:00 news bulletin.", [], "pass"),
    ("Plate posting peaks near 21:00.", [], "pass"),
    ("Plate posts rose towards 22:00.", [], "pass"),
    ("Plate posts piled up during the 20:00 hour.", [], "pass"),
    ("Plate posts ran at a 3:10 ratio.", [], "cut"),
    ("Creators rated plates at a ratio of roughly 1:20.", [], "cut"),
    ("The share moved from 1:20 to 1:10.", [], "cut"),
    ("Plate clips run around 0:45.", [], "cut"),
    ("Plate posts peaked at 21:00 and 1:20 ratio.", [], "cut"),
    ("Plate posts split 3:10 across the platforms.", [], "cut"),
    ("Plate clips held around 1:30 of watch time.", [], "cut"),
    ("Plate posts peaked at 99:99.", [], "cut"),
    ("Plate posts peaked at 13pm.", [], "cut"),
    ("Plate posts peaked at 25h00.", [], "cut"),
    # digits
    ("Plate ratings reached 412 posts.", [412], "pass"),
    ("Plate ratings reached 412 posts.", [], "cut"),
    ("Plate ratings rose 2.8x.", [2.8], "pass"),
    ("Plate ratings rose 2.8x.", [], "cut"),
    ("Plate ratings rose 40%.", [0.4], "pass"),
    ("Plate ratings rose 40%.", [40], "pass"),
    ("Plate ratings rose 40%.", [4], "cut"),
    ("Plate ratings drew 5,000 posts.", [5000], "pass"),
    ("Over 2027 posts rated plates.", [], "cut"),
    ("Plate ratings drew 1.2m views.", [1200000], "pass"),
    ("Plate sales reached R450.", [450], "pass"),
    ("Top 5 K-pop acts joined in.", [5], "pass"),
    # digits with a scale word
    ("Plate ratings drew 412 thousand posts.", [412], "cut"),
    ("Plate ratings drew 412 thousand posts.", [412000], "pass"),
    ("Plate ratings drew 412 hundred posts.", [412], "cut"),
    ("Plate ratings drew 412 dozen posts.", [4944], "pass"),
    ("Plate ratings drew 412 dozen posts.", [412], "cut"),
    ("Plate ratings drew 412 k posts.", [412], "cut"),
    ("Plate ratings drew 412 lakh posts.", [412], "cut"),
    ("Plate ratings drew 412 grand in sales.", [412000], "cut"),
    ("Plate ratings drew 412 thou posts.", [412], "cut"),
    ("Plate ratings drew 412 thousand million views.", [412000000000], "cut"),
    ("A 1.2 million-view clip led.", [1200000], "pass"),
    ("A 1.2 million-view clip led.", [1.2], "cut"),
    ("Plate fans were 2 million-strong.", [2000000], "pass"),
    ("Plate posts were 20 thousand-plus.", [20000], "pass"),
    ("A 5 mn-view clip led.", [5000000], "pass"),
    ("A 5 mn-view clip led.", [5], "cut"),
    ("A 2 bn-view clip led.", [2000000000], "pass"),
    ("Plate posts saw a 40-percent jump.", [0.4], "pass"),
    ("Plate posts saw a 40-percent jump.", [4], "cut"),
    ("Plate posts rose 40 percent.", [0.4], "pass"),
    ("Plate posts rose 40 per cent.", [40], "pass"),
    # spelled figures
    ("Plate ratings drew three hundred thousand views.", [], "cut"),
    ("Plate ratings drew three hundred thousand views.", [300000], "pass"),
    ("Plate ratings drew a million views.", [], "cut"),
    ("Plate ratings drew a million views.", [1000000], "pass"),
    ("Plate ratings drew twelve thousand posts.", [12000], "pass"),
    ("Plate ratings drew half a million views.", [500000], "pass"),
    ("Plate ratings drew half a million views.", [], "cut"),
    ("Plate ratings drew a quarter of a million views.", [250000], "pass"),
    ("Plate ratings drew a quarter of a million views.", [0.25], "cut"),
    ("A thousand thanks poured in for the plates.", [], "cut"),
    ("Plate ratings drew thousands of views.", [], "cut"),
    ("Plate ratings drew thousands of views.", [5000], "pass"),
    ("Plate ratings drew hundreds of thousands of views.", [500000], "pass"),
    ("Plate ratings drew hundreds of thousands of views.", [500000000], "cut"),
    ("Plate ratings drew hundreds of thousands of views.", [500], "cut"),
    ("Plate ratings drew tens of thousands of views.", [50000], "pass"),
    ("Plate ratings drew tens of thousands of views.", [5000], "cut"),
    ("Plate ratings drew a few thousand posts.", [5000], "pass"),
    ("Plate ratings drew a few thousand posts.", [], "cut"),
    ("Plate ratings drew several thousand posts.", [3000], "pass"),
    ("Plate ratings drew several hundred thousand views.", [300000], "pass"),
    ("Plate ratings drew several hundred thousand views.", [300], "cut"),
    ("Plate ratings drew a few hundred thousand views.", [400000], "pass"),
    ("Plate ratings drew a couple of thousand posts.", [2500], "pass"),
    ("Plate ratings drew a couple of thousand posts.", [5000], "cut"),
    ("Plate ratings drew a couple of hundred comments.", [250], "pass"),
    ("Plate ratings drew some thousand posts.", [1500], "pass"),
    ("Plate ratings drew some thousand posts.", [3000], "cut"),
    ("Plate ratings drew a few million views.", [4000000], "pass"),
    ("Forty creators posted plates.", [], "cut"),
    ("Forty creators posted plates.", [40], "pass"),
    ("Twenty-five creators posted plates.", [25], "pass"),
    ("Fifty percent of creators posted plates.", [0.5], "pass"),
    ("Fifty percent of creators posted plates.", [], "cut"),
    ("One in five creators posted plates.", [0.2], "pass"),
    ("One in five creators posted plates.", [], "cut"),
    ("Nine out of ten creators posted plates.", [0.9], "pass"),
    ("Nine out of ten creators posted plates.", [], "cut"),
    ("One out of every five creators posted plates.", [0.2], "pass"),
    ("A quarter of posts rated plates.", [0.25], "pass"),
    ("A quarter of posts rated plates.", [], "cut"),
    ("Two thirds of posts rated plates.", [0.667], "pass"),
    ("Half of the posts rated plates.", [0.5], "pass"),
    ("Half of the posts rated plates.", [], "cut"),
    ("Plate posts doubled this week.", [], "cut"),
    ("Plate posts doubled this week.", [2], "pass"),
    ("Plate posts tripled on Sunday.", [3], "pass"),
    ("Plate posts drew dozens of replies.", [36], "pass"),
    ("A dozen creators posted plates.", [], "cut"),
    # age terms (rule 1), cut by K6 whatever is pinned
    ("Plate fans in their '20s drove it.", [], "cut"),
    ("Plate fans in their ’30s drove it.", [], "cut"),
    ("Plate fans in their 20s drove it.", [], "cut"),
    ("Plate fans in their twenties drove it.", [], "cut"),
    # fifth review: apostrophe decades in a person phrase, and the older decades
    ("Plate fans in their late '20s drove it.", [], "cut"),
    ("Plate fans in their mid-'30s drove it.", [], "cut"),
    ("Late-’20s creators drove the plate posts.", [], "cut"),
    ("The '20s crowd drove the plate posts.", [], "cut"),
    ("Plate fans in their early '20s drove it.", [], "cut"),
    ("Mid-'30s professionals drove the plate posts.", [], "cut"),
    ("Plate fans in their fifties drove it.", [], "cut"),
    ("Plate fans in their 50s drove it.", [50], "cut"),
    ("Plate fans aged between 18 and 24 drove it.", [18, 24], "cut"),
    # fifth review: clocks before event nouns, chains across midnight, the ratio veto
    ("Posts rose after the 18:00 kickoff.", [], "pass"),
    ("The 19:30 episode drove plate talk.", [], "pass"),
    ("Talk rose in the run-up to the 15:00 derby.", [], "pass"),
    ("Posts rose ahead of the 20:00 episode.", [], "pass"),
    ("Late-night posting ran between 23:00 and 01:00.", [], "pass"),
    ("Posting ran from 22:00 to 00:30.", [], "pass"),
    ("TikTok led the split at 21:00.", [], "pass"),
    ("Plate clips lasted about 3:30.", [], "cut"),
    # fifth review: bare shares and multipliers
    ("Nearly half came from TikTok.", [], "cut"),
    ("Nearly half came from TikTok.", [0.5], "pass"),
    ("A third came from Durban.", [], "cut"),
    ("Mentions fell by half.", [], "cut"),
    ("Mentions dropped by a quarter.", [], "cut"),
    ("Engagement was two and a half times last week's.", [], "cut"),
    ("Almost half were posted on Sunday.", [], "cut"),
    ("Views passed the million mark.", [], "cut"),
    ("Views passed the million mark.", [1000000], "pass"),
    ("Posts rose by a factor of three.", [], "cut"),
    ("Plate posts outnumbered pap posts two to one.", [], "cut"),
    ("Posts rose five percentage points.", [], "cut"),
    ("Posts rose five percentage points.", [5], "pass"),
    # fifth review: periods and dish names
    ("Plate posts rose in Q4.", [], "pass"),
    ("Plate posts rose in H2.", [], "pass"),
    ("Plate posts in FY26 beat the prior year.", [], "pass"),
    ("Plate posts rose in the 2026/27 season.", [], "pass"),
    ("Plate posts rose in the 2026-27 season.", [], "pass"),
    ("Plate posts rose in the second quarter of 2026.", [], "pass"),
    ("Plate posts peaked on 27.09.2026.", [], "pass"),
    ("Plate posts peaked on 99.99.2026.", [], "cut"),
    ("Plate clips leaned on 90s kwaito.", [], "pass"),
    ("Plate clips leaned on 90s house.", [], "pass"),
    ("Plate clips ran for 90s each.", [], "cut"),
    ("Plate posts rose from the 21st to the 27th.", [], "pass"),
    ("Plate talk dipped during Covid-19.", [], "pass"),
    ("Posts about 7 colours led the week.", [], "pass"),
    # fifth review: a percent range and space-grouped thousands
    ("Plate ratings rose 40-50%.", [0.4, 0.5], "pass"),
    ("Plate ratings rose 40-50%.", [0.5], "cut"),
    ("Plate ratings drew 120 000 views.", [120000], "pass"),
    ("Plate ratings drew 120 000 views.", [120], "cut"),
# sixth review: a clock after its time noun or verb
    ("The peak was 21:00 on Sunday.", [], "pass"),
    ("Kick-off is 15:30 on Saturday.", [], "pass"),
    ("Plate posts peaked 21:00 on Sunday.", [], "pass"),
    ("Peak hour for plate posts was 21:00.", [], "pass"),
    ("Kickoff: 15:30, FNB Stadium.", [], "pass"),
    ("The show airs weeknights 18:30 on SABC.", [], "pass"),
    # sixth review: ascending spelled ranges, and idioms
    ("Most creators posted two to three plates a day.", [2, 3], "pass"),
    ("Most creators posted two to three plates a day.", [], "cut"),
    ("Nine to five workers posted plates at lunch.", [], "pass"),
    # sixth review: elder terms (rule 1)
    ("Gogos drove the plate posts in Soweto.", [], "cut"),
    ("The dancing gogos went viral again.", [], "cut"),
    ("Grannies drove the plate posts.", [], "cut"),
    ("Grandparents drove the plate posts.", [], "cut"),
    ("Grandmothers drove the plate posts.", [], "cut"),
    # sixth review: half-million, a quarter million, durations
    ("Views crossed the half-million mark.", [], "cut"),
    ("The clip drew half-million views.", [], "cut"),
    ("Streams topped a quarter million.", [250000], "pass"),
    ("Streams topped a quarter million.", [0.25], "cut"),
    ("Plate clips ran about 2:30 each.", [], "cut"),
    ("Plate clips run around 2:30.", [], "cut"),
    ("Posts rose from 18:00 over the long weekend.", [], "pass"),
    ("Mentions rose three-quarters on the week.", [], "cut"),
    ("Posts rose by a hundred and fifty.", [100], "cut"),
    ("Posts rose by a hundred and fifty.", [150], "pass"),
    # sixth review: minors
    ("A third wheel joke ran through the plate posts.", [], "pass"),
    ("One half of the duo posted plates.", [], "pass"),
    ("Posts rose 40\u201350%.", [0.4, 0.5], "pass"),
    ("Every second post mentioned load-shedding.", [], "cut"),
# seventh review: birth cohorts (rule 1)
    ("Creators born in the 2000s rated plates.", [], "cut"),
    ("The 2000s generation rated plates.", [], "cut"),
    ("The '90s generation rated plates.", [], "cut"),
    ("Creators who grew up in the 1990s rated plates.", [], "cut"),
    ("Creators born in 1998 rated plates.", [], "cut"),
    # seventh review: at, by and until never beat a ratio word
    ("The reply to like ratio stood at 1:20.", [], "cut"),
    ("The rice to pap split came in at 3:10.", [], "cut"),
    ("The odds sat at 2:15 for Pirates.", [], "cut"),
    # seventh review: more time nouns
    ("The best time to post a plate was 19:00.", [], "pass"),
    ("The most common posting time was 20:00.", [], "pass"),
    ("Prime time, 20:00, carried the most plate posts.", [], "pass"),
    ("Plate posts bunched at lunchtime (13:00).", [], "pass"),
    # seventh review: elder and child terms in the markets' languages (rule 1)
    ("Ugogo reaction videos drew the most shares.", [], "cut"),
    ("Ogogo shared their Sunday plates.", [], "cut"),
    ("Makhulu posted a plate from Gqeberha.", [], "cut"),
    ("Watoto shared plates in Nairobi.", [], "cut"),
    # seventh review: half a million after a hedge, event nouns before a ratio, more time verbs
    ("Plate clips drew about half a million views.", [500000], "pass"),
    ("Plate clips drew nearly half a million views.", [0.5, 1000000], "cut"),
    ("After the derby, replies outnumbered likes 2:45.", [], "cut"),
    ("During the Soweto derby, Chiefs fans outposted Pirates fans 3:10.", [], "cut"),
    ("Most posts go live 13:00 on a Sunday.", [], "pass"),
    ("Engagement drops off after 22:00 and picks up again 06:00.", [], "pass"),
    ("Most activity comes 19:00 onwards.", [], "pass"),
    # seventh review: a d/m date inside the window, trading hours, a mark in a clip
    ("Posts climbed on 24/09.", [], "pass"),
    ("Stalls open eight to five on Saturdays.", [], "pass"),
    ("Replies spiked at the 2:30 mark of the clip.", [], "cut"),
    # live Gemini staging, 29 September: an order word before an adjective and a noun is no share
    ("Leisure outings formed a third prominent thread.", [], "pass"),
    ("Plate talk became a third topic after the derby.", [], "pass"),
    ("A third said they rated the plates.", [], "cut"),
    ("A third of posts rated plates.", [], "cut"),
    # review round 1: a verb or a plural after the fraction keeps it a share
    ("Two thirds shared stories about load-shedding.", [], "cut"),
    ("A quarter joined conversations about the strike.", [], "cut"),
    ("A third started threads on the fuel price.", [], "cut"),
    ("A third said times are hard.", [], "cut"),
]


def make_ctx(entries):
    ctx = RunContext(run_id=RUN, tier="T1", as_of=datetime.fromisoformat(AS_OF), market="ZA")
    for r in STORED:
        ctx.evidence[r["id"]] = copy.deepcopy(r)
    rows = [{"day": "2026-09-27", **{f"v{i}": v for i, v in enumerate(entries)}}]
    ctx.record_query(SQL, {"tag": "7colours"}, rows, "corpus figures")
    return ctx, rows


class Warehouse:
    def __init__(self, rows):
        self.rows = rows

    def run(self, sql, params, max_bytes_billed):
        return copy.deepcopy(self.rows)


def make_draft(ctx, entries):
    q = ctx.queries["q_1"]
    numbers = [{"value": v, "unit": "corpus figure", "query_id": "q_1", "run_id": RUN, "result_hash": q["result_hash"]}
               for v in entries]
    return {
        "status": "complete",
        "as_of": AS_OF,
        "short_answer": "Sunday lunch plate ratings rose on TikTok and X this week.",
        "claims": [
            {"id": "c1", "text": "People on TikTok and X rated their Sunday lunch plates on 27 September.",
             "label": "corroborated", "kind": "observation", "evidence_ids": ["tt_1", "x_1"]},
            {"id": "c2", "text": "Creators on TikTok called \"Pap AND rice on one plate\" a crime.",
             "label": "observed", "kind": "observation", "evidence_ids": ["tt_1", "tt_2"],
             "quotes": [{"evidence_id": "tt_2", "text": "Pap AND rice on one plate is a crime"}]},
            {"id": "c3", "text": "Sunday plate rating looks like a weekly ritual.", "label": "inferred",
             "kind": "interpretation", "evidence_ids": ["tt_1", "x_1"], "numbers": numbers},
        ],
        "evidence": [copy.deepcopy(ctx.evidence[i]) for i in ("tt_1", "tt_2", "x_1")],
        "so_what": [],
        "watch_next": [],
        "gaps": [],
    }


def verdict_in(where, text, entries):
    ctx, rows = make_ctx(entries)
    draft = make_draft(ctx, entries)
    if where == "short_answer":
        draft["short_answer"] = text
    elif where == "claim":
        draft["claims"][2]["text"] = text
    else:
        draft["gaps"] = [{"what": text, "searched": "stored posts", "why": "no posts found"}]
    key = {"short_answer": "short_answer", "claim": "c3", "gap": "gaps/0"}[where]
    _, verdicts = check_answer(draft, ctx, Warehouse(rows), window=WINDOW, markets=["ZA"])
    return "cut" if any(v["claim_id"] == key and v["verdict"] == "cut" for v in verdicts) else "pass"


def test_the_corpus_holds_about_150_distinct_rows():
    assert len(CORPUS) >= 150
    assert len({(t, tuple(e)) for t, e, _ in CORPUS}) == len(CORPUS)


def test_the_baseline_draft_passes_every_check():
    for where in ("short_answer", "claim", "gap"):
        assert verdict_in(where, "Sunday plate rating looks like a weekly ritual.", []) == "pass"


@pytest.mark.parametrize("where", ["short_answer", "claim", "gap"])
@pytest.mark.parametrize("text, entries, expected", CORPUS)
def test_phrase_corpus(where, text, entries, expected):
    assert verdict_in(where, text, entries) == expected
