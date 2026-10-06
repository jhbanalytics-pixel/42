# Twitter/X live probe, 2026-07-02

Closes flip-readiness audit row 084 (the open /twitter/user/tweets probe). Read-only probe against EnsembleData with the production token, 6 units spent total, verified by `fetch_units_history` before and after (twitter 0 -> 6 on 2026-07-02).

## Result: token CAN access Twitter

No 403, no 493 subscription error. The current plan includes the Twitter endpoints. Twitter is dark today purely on our side: `twitter_handles_enabled: false` and `twitter_handles: []` in all three markets in `configs/sources.yaml`, so `_maybe_wire_twitter_endpoints` never appends the endpoint and even a flag flip alone would be a silent no-op with the empty handle list.

## Calls made

1. `GET /twitter/user/info?name=News24` -> 200. `data.rest_id = "14697575"`, `data.legacy.screen_name = "News24"`. 2 units (matches docs pricing).
2. `GET /twitter/user/tweets?id=14697575` -> 200. `data` is a list of 99 timeline entries. 4 units (matches docs pricing).

## Response shape (/twitter/user/tweets)

GraphQL timeline envelope, exactly as flip-readiness row 084 predicted:

```
data[i].content.itemContent.tweet_results.result
  .rest_id                      tweet id
  .core.user_results.result.legacy.screen_name
  .legacy.created_at            "Wed Oct 30 05:45:03 +0000 2019" (Twitter v1.1 format)
  .legacy.full_text             tweet body
  .legacy.favorite_count        14339 (sample)
  .legacy.retweet_count         1991
  .legacy.reply_count           224
  .legacy.quote_count, .legacy.bookmark_count also present
```

Metrics are populated and non-null. `created_at` parsing already fixed (#99). The connector's existing pick-lists in `ensemble.py` already cover `legacy.full_text`, the Twitter author shapes, and the twitter URL builder, but the row extraction expects post dicts; confirm during flip that `_fetch_endpoint` unwraps the `content.itemContent.tweet_results.result` nesting or the run lands zero rows. Note the vendor returns tweets in Twitter's order, not chronological; the sample first entry was a 2019 pinned/highlight tweet, so recency filtering must key off `legacy.created_at`, not list order.

## Cost

Per handle per cron run: 2 units resolve (cached process-level, so once per run per handle) + 4 units per tweets call. 5 handles per market x 3 markets = 15 handles = 30 units tweets + up to 30 units resolve = ~60 units/day worst case, ~1.2% of the 5000/day cap. Vendor truth on 2026-07-02: tiktok 163 + instagram 1036 + reddit 629 + threads 620 = 2448 units before this probe, so headroom is comfortable.

## Flip checklist (Track D, in order)

1. D1 legal: WPP/Ogilvy compliance sign-off for X data in a Google-facing product. HARD GATE, no recurring spend before it clears (v3.8 Track D constraint). This probe was a one-off technical verification.
2. D2: shortlist ~5 verified X handles per market and populate `twitter_handles` in sources.yaml (protected file, Albert edits or explicit confirm). The 2-unit resolve doubles as the existence check.
3. D3: done (this note).
4. D4: flip `twitter_handles_enabled: true` for ZA only, one cron, morning-check + `fetch_units_history` delta, confirm tweets land with metrics and non-null published_at. Verify the row-extraction nesting point above on that first run.
5. D5: NG then KE on separate days. One charging surface per day rule applies.

## Legal + flip status

WPP legal approved 2 Jul 2026 (Albert), D1 gate cleared. ZA flipped this date: `twitter_handles_enabled: true` with 5 verified handles (News24 14697575, casspernyovest 183253150, Soccer_Laduma 59110226, DjMaphorisa 182573602, AdvoBarryRoux 2449502355). kulanicool was shortlisted but /twitter/user/info returns `data: null` (renamed or gone), replaced with AdvoBarryRoux. GraphQL row-extraction unwrap + legacy metric candidates shipped in the same commit with a unit test on the real probe envelope. NG and KE stay dark for separate cron days.
