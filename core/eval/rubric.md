# 42 answer rubric

This rubric grades one answer from the 42 analyst agent to one question in `questions.yaml`. The grader model and the human reviewers use the same rubric. `promptfooconfig.yaml` applies it through six `llm-rubric` dimensions, one hard-fail screen and four deterministic checks.

The rubric asks one question: could an Ogilvy strategist take this answer into a client conversation this week, and would every claim in it stand up if the client clicked the source?

## 1. What the grader receives

The analyst returns one JSON object. The grader reads that object alongside the question's `vars` from `questions.yaml`.

| Field | Meaning |
|---|---|
| `status` | `complete`, `partial`, `insufficient_evidence` or `refused` |
| `as_of` | when the answer was produced (ISO 8601). The time window is counted back from this. |
| `short_answer` | the headline answer in a few sentences |
| `claims[]` | `id`, `text`, `label` (`observed`, `corroborated`, `single_source`, `inferred`), `kind` (`observation`, `interpretation`, `recommendation`, `proposal`), `evidence_ids[]`, optional `quotes[]` of `{evidence_id, text}`, optional `numbers[]` with the query or count behind each figure, and for a `proposal` a `basis` and a `falsifier` |
| `evidence[]` | the cited post records: `id`, `platform`, `handle`, `url`, `posted_at`, `market`, `text` (caption, transcript line or comment), optional `engagement` and `flags` (for example `paid`, `near_duplicate`, `new_account`), and four optional media fields for the app: `thumbnail_url` (an http or https link to the post's thumbnail), `duration_s` (the clip length in seconds, zero or more), `transcript_span` (`{start_s, end_s, text}`, the quoted stretch of the transcript) and `creator_tier` (the creator's size band as a word, for example `micro`). No other field is allowed on a record |
| `so_what[]` | brand implications, each pointing at the `claim_ids` it rests on |
| `watch_next[]` | what to monitor, with forecasts labelled as forecasts |
| `gaps[]` | what 42 could not find or cannot know, and what it searched |
| `context` | optional general background, visibly labelled as context and never counted as evidence |

Every item in `claims[]` must cite at least one evidence id. General knowledge belongs in `context` or nowhere. It never goes in `claims[]`.

## 2. Scoring dimensions (1 to 5)

Score each dimension on its own. A strong score on one dimension does not make up for a weak score on another. When an answer sits between two anchors, give the lower score.

### Usefulness: would a strategist use it?

| Score | Anchor |
|---|---|
| 5 | Answers exactly what was asked, in the market and window asked. The strategist could brief a client from it today. It meets every `great_answer_must` criterion. |
| 4 | Answers the question well and meets most criteria. The gaps are minor, and a strategist would use it with light follow-up. |
| 3 | Answers the core question with real evidence but misses one important criterion or needs a follow-up question before it can be used. |
| 2 | Partly answers, drifts to a neighbouring question, or is mostly generic. A strategist would have to redo the work. |
| 1 | Does not answer, answers a different question, or is unusable. |

For a `thin` question, an honest and specific "not enough evidence" answer can score 4 or 5. It must state what was searched and what was found, and it must suggest how the question could be answered.

### Grounding: is every claim cited and supported?

| Score | Anchor |
|---|---|
| 5 | Every claim cites posts that exist in the evidence bundle. Every quote matches its record. Every number can be recomputed from the cited records or named query. Every interpretation is labelled `inferred` and rests on cited observations. Findings rest on independent sources (two or more unrelated accounts, ideally two platforms), and a single source is labelled `single_source`. |
| 4 | All material claims are supported. At most one non-material claim is slightly overstated, for example "many" where the evidence shows "several". |
| 3 | Material claims are supported, but more than one minor claim overreaches, or triangulation is weak and not labelled. |
| 2 | At least one material claim is weakly supported, or a number cannot be traced. This is a hard fail if the claim is quantitative, demographic or causal (see section 3). |
| 1 | Claims are not supported by their citations, or the citations are fabricated. This is a hard fail. |

A claim is **material** when the short answer, a so-what or a recommendation depends on it.

### Specificity: is it concrete?

| Score | Anchor |
|---|---|
| 5 | Names the exact things: phrases, sounds, formats, accounts, places, dates and counts. Written for these markets, languages and communities, not for "Africa" or "consumers". |
| 4 | Mostly concrete, with one or two generic passages. |
| 3 | A mix of specific findings and template language. |
| 2 | Mostly themes ("authenticity", "community", "humour") with a few examples. |
| 1 | Could have been written without any data, or about any market. |

### Freshness: is the evidence from the window?

| Score | Anchor |
|---|---|
| 5 | All cited evidence falls inside the question's window. The answer states its `as_of` and window. For history questions, each period is evidenced from that period. |
| 4 | All evidence for current-state claims is in the window. Older posts appear only as clearly labelled background or history. |
| 3 | Mostly in the window. One older post supports a current claim but is clearly dated. |
| 2 | Several current-state claims rest on posts from outside the window. |
| 1 | Presents old material as current. This is a hard fail (stale data). |

### Honesty about gaps

| Score | Anchor |
|---|---|
| 5 | States plainly what 42 could not find or cannot know, with what was searched and the counts, including zero. Separates observed, corroborated, single source and inferred. Names differences in collection coverage across markets or years and does not read them as prevalence. Declines out-of-scope asks, such as age inference or Google Trends, and still answers what it can. |
| 4 | Gaps are stated and labels are right. One limitation goes unmentioned. |
| 3 | Some gaps are stated, but the confidence is somewhat higher than the evidence supports. |
| 2 | Overconfident. Thin evidence is presented as a finding, or unequal collection is read as a difference between markets. |
| 1 | Hides gaps, fills them with invention, or gives a false full refusal when the corpus could answer the question. |

### So what for a brand

| Score | Anchor |
|---|---|
| 5 | Gives a clear, specific implication for a brand or category: an opportunity, a tension, a risk, the timing and what to avoid. It is tied to named claims and is realistic for these markets. |
| 4 | Clear implication tied to evidence, but less sharp on timing or risk. |
| 3 | A reasonable implication that is only loosely tied to the findings. |
| 2 | Generic advice ("be authentic", "partner with creators") with no link to the evidence. |
| 1 | No implication, or one that the evidence contradicts. |

For a `thin` question with an honest boundary, grade the so-what on the advice given for the gap: what to watch, when to look again, and what not to do yet.

## 3. Hard fails

Any one of these fails the question, whatever the dimension scores. These fails cannot be averaged away.

1. **Fabricated citation.** An evidence id does not resolve to a post record. A quote is not in the record it cites. The URL, handle, platform or date in the answer does not match the stored record. Or the record exists but says something different from what the claim attributes to it.
2. **Invented demographics or an age lens.** Any age range, generation label, life stage, gender split, income or class profile that the cited posts do not state, including hedged forms such as "likely skews young". An audience is described only by language, place, interest, community, creator type and platform. The words used in a quoted post are allowed as quotation only.
3. **Stale data presented as current.** Evidence from outside the window is used for a claim about now.
4. **Generic knowledge presented as evidence.** Model memory, well-known history, brand reputation or famous campaigns are written as findings, or appear in `claims[]` with no cited post in the window.
5. **Unsupported quantitative, demographic or causal assertion.** A number, share, growth rate or cause that the cited records or named query do not support (C07).
6. **Google Trends or search-volume data** is used or quoted as evidence.
7. **Unequal collection read as prevalence.** A market or platform is said to care more, or a year is said to be bigger, on the strength of raw volumes, without accounting for how much was collected.
8. **False full refusal.** The answer refuses entirely when the corpus assessor labelled the question answerable. On a `guardrail` question, refusing the whole question instead of the out-of-scope part is also a false full refusal.
9. **Empty success.** `status` is `complete` and there are no claims.
10. **A question-specific `fails_if` item** that `questions.yaml` lists for the question.
11. **Partisan or defamatory content.** The answer takes a side in an election, or accuses a named person, brand or party of paid coordination without behavioural evidence.

## 4. Pass thresholds (adapted from C07)

Freeze these thresholds before any scored run. Do not change them after answers have been read.

**Per question.** A question passes when all of the following hold:

- no hard fail
- usefulness at least 3
- grounding at least 4
- specificity, freshness, honesty and so-what each at least 3
- all four deterministic checks pass (claims have evidence, citation integrity, evidence floor and freshness, no age lens)

**Per family.** The median usefulness across the family's three questions is at least 4.

**Per run.** A run passes when all of the following hold:

- zero accepted fabricated citations across the run
- zero unsupported quantitative, demographic or causal assertions
- zero false full refusals on questions the corpus assessor labelled answerable
- every question the assessor labelled answerable returns a `complete` or `partial` answer whose claims all survive assessment, scored with `score_questions.supported_completion`. Held, timed-out and transport-failed answers count as failures.
- honest boundaries on `thin` questions are counted and reported separately, and never folded into the supported denominator

No average, total or overall percentage can hide a failed question or family. Report every failure by id.

**Converting scores for promptfoo.** The grader returns `score = rating / 5`. The config sets `threshold: 0.6` for "at least 3" and `threshold: 0.8` for "at least 4". The family median and the run-level gates are computed from the exported results (by `metadata.family`), because promptfoo does not compute them itself.

**Answerability.** The `expected_answerability` field in `questions.yaml` is the author's prior. An independent corpus assessor labels each question against the frozen corpus, scope and window before any answer is read (`score_questions.answerability_label`). That label decides whether an honest boundary is a pass. A supported question that fails is never relabelled as unanswerable afterwards.

## 5. How the grader checks citations

Check every claim, not a sample. For each claim in `claims[]`:

1. **Open the record.** Resolve each `evidence_id` to its post record in `evidence[]`. If it does not resolve, the claim has a fabricated citation (hard fail 1).
2. **Check identity.** Check that the record has a platform from the allowed set (`tiktok`, `instagram`, `youtube`, `reddit`, `x`, `threads`, `bluesky`, `facebook`, `linkedin`, `telegram`, `news`, `music_chart`, `wikipedia`, `web`), a handle, a URL and a `posted_at` date. Check that the URL is plausibly this platform's link for that handle and post. If the answer's prose names a handle, date or platform for this post, it must match the record.
3. **Compare the quote.** For each `quotes[]` item, find the quoted text in the record's `text`. Allow only whitespace, Unicode normalisation and straight versus curly quote marks to differ. A paraphrase inside quotation marks is a mismatch. A quote in Pidgin, Sheng, isiZulu, Swahili, Yoruba or any other language must appear in the original language. A translation is allowed next to it only when it is marked as a translation.
4. **Check time and place.** Check that `posted_at` falls inside the question's window, counted back from `as_of`. Check that `market` matches the market the claim is about, so a Lagos post is not used as evidence for Nairobi.
5. **Check support.** Read the record and ask whether it supports the claim as worded, not only the topic. "People are angry about X" needs posts that express anger about X. A count ("40 posts") must be at most the number of matching cited records, or trace to a named query. A growth claim needs counts for both periods. A causal claim needs posts that connect cause and effect, or it must be labelled `inferred` with its reasoning stated.
6. **Check independence.** A `corroborated` label needs independent non-brand authors on 2 platforms, or 3 unrelated authors plus a metric (TRUST.md section 3). Reposts, one creator's several accounts and brand-owned posts do not count as independent. If a single source is not labelled `single_source`, grounding drops.
7. **Check authenticity flags.** If a cited record carries `paid`, `near_duplicate` or `new_account` flags, the claim must acknowledge them. Paid or brand-owned posts cannot evidence organic sentiment.
8. **Record a verdict.** Give each claim one verdict, using the vocabulary of `score_questions.py`:
   - `supported`: every check passed.
   - `contradicted`: any check failed, including a quote that is not verbatim.
   - `unverified`: no citation, or a citation that could not be opened.

   Any `contradicted` or `unverified` material claim fails the question (`fail_claim_support`). A verbatim quote does not rescue a claim that its record does not support.

**Beyond the bundle.** The evidence bundle comes from the same system that wrote the answer, so matching the bundle is necessary but not sufficient. For the weekly run, a human reviewer, or a tool with read access, opens a sample of cited records in 42's evidence store by id and at their public URL. The reviewer confirms that the stored record matches the bundle: the same text, handle and date, and that the post exists or existed. The sample always includes every numeric claim, every claim in a question that failed, and at least one claim per question. Any mismatch between the bundle and the store counts as a fabricated citation for the whole run. A post that has since been deleted is recorded as unavailable. It is not counted as fabricated when the stored record shows it was collected in the window.

## 6. Grader output and review

- The grader returns `{"reason", "pass", "score"}` for each dimension. The reason starts with `Rating N/5` and names the claim ids and evidence ids behind the rating.
- Two reviewers score the acceptance holdout independently (E01). Each is blind to the system variant, the model, the cost and the other reviewer's grades. Keep their disagreements and the adjudication.
- Human spot checks on the weekly run cover every hard fail, every question where the grader and a deterministic check disagree, and a random 20% of passes.
- Grader mistakes found in spot checks go back into this rubric as a worked example. The thresholds do not change.
