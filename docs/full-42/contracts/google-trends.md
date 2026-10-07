# Google search interest strip

The `SearchingNow` consumer accepts an optional `searching_now` array on the existing `/api/today` and `/api/discover` response envelopes. Since 3 October 2026 f42-api emits it on both, read from intelligence_42_core.google_search_signals; core/api/contract.md section 21 sets which rows are read, how they are scoped to the market and dated, and how a missing table or failed read falls back to an empty list. Live rows still depend on the collect job's appends reaching that table on staging.

Each row has exactly five fields:

| Field | Accepted value | Display rule |
|---|---|---|
| `term` | Non-empty string | Render as text. |
| `market` | `ZA`, `NG`, or `KE` | Keep only rows matching the selected market, or group all valid markets in `ZA`, `NG`, `KE` order when the selection is `ALL`. |
| `source` | `google_bq` or `google_trending` | Validate the source value; the strip does not infer other source details. |
| `rank` | Positive integer or `null` | Show a positive value as `Reported rank N`. Omit rank when the value is `null`. The label makes no national-ranking claim. |
| `refreshed_at` | Calendar date `YYYY-MM-DD` or UTC ISO timestamp ending in `Z` | Render the supplied value at its supplied precision. A date-only value stays date-only. |

The consumer rejects rows with missing, extra, or malformed fields. It does not consume original region, week, raw producer rank, scores, traffic bands, URLs, or other producer data. It does not derive a timestamp from a BigQuery date or add a value when a field is absent.

The strip heading is `Trending on Google` (it was `Searching now` until 6 October 2026, which a tester read as a page still loading) and its caption, under the heading, is `Google search interest, not posts`. When any row has a rank, a note under the caption says a rank is the term's place on Google's own list, so some numbers are skipped; each rank reads `Google rank N` after the row's freshness. Valid rows appear in market groups, with ranked rows ordered by reported rank and other rows ordered deterministically after them. An absent, empty, invalid, or market-mismatched array returns no strip. The strip does not create a standalone card.
