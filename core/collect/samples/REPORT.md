# SocialCrawl probe report

Run probe-20260928T191308Z, written 2026-09-28 19:32 UTC by core/collect/probe_report.py.
Charged is what the ledger booked; reported is the vendor's credits_used from the response body.

| probe | route | status | charged | reported | rows |
|---|---|---|---|---|---|
| 0a | credits/balance | 200 | 0 | 0 | 0 |
|  | credits/balance | no response | 0 | n/a | 0 |
| 0b | status | 200 | 0 | n/a | 0 |
| 0c | utility/capabilities | 200 | 0 | 0 | 0 |
| 1-za | tiktok/trending | 200 | 5 | 5 | 24 |
| 1-ng | tiktok/trending | 200 | 5 | 5 | 26 |
| 1-ke | tiktok/trending | 200 | 5 | 5 | 8 |
| 2-ng | youtube/videos/trending | 200 | 1 | 1 | 50 |
| 2-za | youtube/videos/trending | 200 | 1 | 1 | 50 |
| 2-ke | youtube/videos/trending | 200 | 1 | 1 | 49 |
| 3 | tiktok/hashtags/popular | 200 | 6 | 6 | 3 |
| 4-ng | apple_music/charts | 200 | 1 | 1 | 50 |
| 4-ke | apple_music/charts | 200 | 1 | 1 | 50 |
| 5 | search/news | 200 | 4 | 4 | 16 |
| 6a | twitter/search/tweets | 200 | 0 | 0 | 0 |
| 6b | twitter/search/tweets | 200 | 1 | 1 | 2 |
| 7 | youtube/search/advanced | 200 | 1 | 1 | 50 |
| 8a | instagram/search/location | 200 | 5 | 5 | 30 |
| 8b | instagram/location/posts | 200 | 5 | 5 | 23 |
| 9a | facebook/events | 200 | 0 | 0 | 0 |
| 9b | facebook/search/posts | 200 | 1 | 1 | 5 |
| 10a | search/multi | 200 | 0 | 0 | 0 |
| 10b | search/multi | 200 | 6 | 6 | 78 |
| 11a | tiktok/search/top | 200 | 1 | 1 | 16 |
| 11b | tiktok/search/top | 200 | 0 | 0 | 0 |
| 12b | tiktok/hashtag | 200 | 1 | 1 | 0 |
| 13 | prism/post-stats | 200 | 3 | 3 | 3 |
| 14-za | web/scrape | 200 | 1 | 1 | 0 |
| 14-ng | web/scrape | 200 | 1 | 1 | 0 |
| 14-ke | web/scrape | 200 | 1 | 1 | 0 |
| 15 | credits/transactions | 200 | 0 | 0 | 50 |

Total charged: 57 credits over 31 ledger rows.

Not called (skipped or stopped): 12a.

## TikTok feed=local: share of videos whose ext.region is the market

- ZA: 20 of 24 (83%)
- NG: 17 of 26 (65%)
- KE: 1 of 8 (12%), under 40%

## NG and KE feeds

The feeds overlap: NG and KE share 1 video ids (26 NG, 8 KE).

Under 40% local or overlapping feeds: per BUILD.md 0.5, move that budget to the X trends seed, hub panels and country-filtered search before task 1.3.

## Comment, transcript and parser bodies

Run probe-bodies-20260929T101355Z. Redacted bodies are in core/collect/tests/fixtures/socialcrawl_bodies.json.

| probe | group | route | status | charged | reported | rows |
|---|---|---|---|---|---|---|
|  |  | credits/balance | no response | 0 | n/a | 0 |
| c1 | comments | tiktok/post/comments | 200 | 1 | 1 | 49 |
| c2 | comments | instagram/post/comments | 200 | 5 | 5 | 15 |
| c3 | comments | youtube/video/comments | 200 | 1 | 1 | 100 |
| c4 | comments | reddit/post/comments | 200 | 5 | 5 | 11 |
| c5 | comments | twitter/tweet/replies | 200 | 1 | 1 | 36 |
| t1 | transcripts | youtube/video/transcript | 200 | 3 | 3 | 0 |
| t2 | transcripts | tiktok/post/transcript | 404 | 10 | 0 | 0 |
| t3 | transcripts | instagram/media/transcript | 400 | 10 | 0 | 0 |
| t4 | transcripts | twitter/tweet/transcript | 400 | 10 | 0 | 0 |
| p1 | parsers | tiktok/search/hashtag | 200 | 1 | 1 | 14 |
| p2 | parsers | tiktok/profile/videos | 200 | 1 | 1 | 10 |

Comments: 13 credits. Transcripts: 33 credits. Parsers: 2 credits. Total: 48 credits.

Not called (skipped or stopped): t5, p3.
Field names kept that are not in the Stage 0 vocabulary; read them for a handle before committing the fixture: abstained, at_cut, author_channel_id, author_following, author_posts_count, author_url, baseline, cadence, choice, comment, comment_language, comment_recency, complaint, counted, creator_baseline, details, doc_url, en, es, high, judged_by, label_share, lang, low, low_confidence, metric, n, negative, neutral, nulls, offset, parent_id, policy_version, positive, post_id, pt, purchase_intent, question, quote_count, ratio, reason, receipts, replies, replies_token, retryable, rows_used, ru, script, sentiment, share, speechRate, text_original, too_few, transcript, truncated, viewer_rating, vs_creator, wordCount.
