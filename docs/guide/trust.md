# How 42 earns trust

42's first requirement is that nothing it shows a strategist is unsupported. This guide lists every check a Today card or an Ask answer goes through, as the code implements it. [Back to the README](../../README.md).

## 1. The publish gate for Today

`core/trust/gate.py` runs these rules in order, and the first hold wins:

| Rule | Holds the card when | In short |
|---|---|---|
| G1 | Any of the last three market-days had invalid data on the main platform | Not enough clean data |
| G3 | The item was seen in no measured feed, only by search | Found by search only |
| G6 | The share of posts located in the market is unknown or inconsistent, or at most half | Market unconfirmed, or Global |
| G5 | Half or more of the posts are paid or sponsored | Paid-led |
| G5b | The item is on the campaign hashtag list | Campaign tag |
| G4b | It is political with no independent corroboration | Not assessed |
| G8 | It is seasonal | Goes to Moments |
| G10 | The explanation failed its checks | Held |

The brief job (`core/brief/job.py`) adds its own holds: a platform-generic tag, no readable name, a paid tag (#ad, #collab and similar) as the item's key, likely coordinated posting, fewer than three posts it can show, and fewer than two local posts.

A post counts as paid when it carries a sponsored flag, a whole hashtag such as #ad, #sponsored or #paidpartnership, or the words "paid partnership" (`core/brief/gatectx.py`).

Held items are never dropped. Today lists each one with its rule, a fixed plain-language reason and its posts and figures. When more than 30% of a market's candidates are held for data reasons, a banner says so. The reason text is fixed per rule, never written by a model.

## 2. How a card is built

For each market the brief takes ten candidates from a pool of 90, ranked local first: eligible items, then how local they are, then whether at least two local creators are posting, then worth attention (`core/brief/sql/brief.sql`). Items held only by G1 do not use up a slot. An item with fewer than three new posts merges into an overlapping card as "also".

Each candidate gets one cross-platform confirm search (presence only, never evidence), then one cited explanation. The explanation must rest on at least two local posts and one exact local quote of 2 to 25 words (`core/brief/specificity.py`). The writer gets one repair round. Only explained cards that pass every check stay on Today. The run stops at the 06:15 deadline or the daily model cap, whichever comes first.

## 3. The claim checks

Every claim in a card or an answer goes through code checks (`core/trust/claims.py`, and `core/agent/checks.py` for Ask):

| Check | What it enforces |
|---|---|
| K1 | Each claim id is unique, every evidence id resolves, and every quote is at least two words and matches the post word for word |
| K2 | Every number in the text matches a numbers entry pinned by query id, run id and result hash. On re-run, counts must match exactly and ratios within 2% |
| K3 | Every cited post sits inside the time window, and none is located in another market |
| K5 | The confidence label is lowered to what the evidence allows |
| K6 | No banned terms (age and generation words, search-interest wording) and no generated evidence |
| K8 | Translations are marked as translations |
| K10 | The answer drops to partial, or to insufficient evidence when fewer than two claims survive |

Every verdict is written to the `claim_checks` table, so any claim can be traced back to why it passed.

## 4. Confidence labels

Labels are computed by code, never chosen by a model:

| Label | Needs |
|---|---|
| Corroborated | Independent authors on two platforms, or three authors plus a pinned number |
| Observed | Two independent authors |
| Single source | One author |
| Inferred | An interpretation. Interpretations always stay Inferred |

Brand accounts, paid or sponsored posts, near-duplicates and generated posts do not count as independent authors.

## 5. Two independent reviews

**Support checker.** One check per surviving claim and one on the explanation sentence. Only "supported" passes; "partial" is a cut.

**Critic.** Names the simplest non-cultural explanation: one viral post, one creator, a news story, a scheduled event. The card passes only when that explanation is ruled out and a local why-now is confirmed. A news-driven or scheduled topic can still pass when at least two distinct local creators are reacting in their own words; it is then labelled news-driven and every claim drops one label step.

In Ask, deep reads add the critic after the researchers, and it can only lower or cut, never add.

## 6. Locality

How a post is placed in a market (`core/detect/geo.py`), strongest first: the platform's own region, the creator's home market or profile location, place names in the text, then language. A post counts as located at confidence 0.7 or above.

- A post located in the market counts in full.
- A post from the market's own feed (a region-scoped feed, the market's subreddits, curated local creator and gossip pages) without a known place counts one step lower, and supports only feed wording such as "seen in Kenya's feeds".
- A post located in another market never counts. The writer sees it marked as not citable, and K3 cuts any claim that cites it.

## 7. What is never evidence

- Generated text, images or video.
- Search interest. Daily trending searches guide what 42 looks into. The route returns signals only, with no evidence ids, and search-interest wording is a banned term in claims.
- News headlines. They become seeds and power the news-to-social bridge.
- Platform boards and charts. They are shown as the platform's own list, not as 42's finding.

## 8. No age lens

42 never infers age. Age, generation and demographic terms are breaches in both the brief and Ask checks, and the writer is told never to infer age. Audiences are described by language, place, interest, community, creator type and platform.

## 9. Measuring itself

A 30-question set across ten families and three markets (`core/eval/questions.yaml`) is scored in replay mode from cached responses, so evaluation spends no live credits. Assertions include citation integrity, an evidence floor and the no-age-lens rule. A weekly quality score (0 to 100 per market) combines eval pass rate, claim support, honest gaps, forecast skill and reviewed precision; any part without enough data is marked insufficient, never zero. The detection scorecard on Coverage reports time to detect, lead time, precision, recall, breadth and cost per confirmed trend.

## Known gaps

Written down so nobody assumes otherwise:

- The statistical test behind Rising needs 14 observed days per series, so Rising cannot fire during warm-up.
- The support checker has no calibrated threshold yet.
- Rules G7 and G9 and check K7 from the original specification are not implemented, and K4 is not calibrated.
- G4b does not yet require a clear authenticity result.
- The campaign hashtag list is empty.
