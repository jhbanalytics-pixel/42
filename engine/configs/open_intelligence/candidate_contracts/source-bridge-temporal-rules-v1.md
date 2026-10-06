# Bridge v3 temporal rules, reviewed list v1

The temporal rules artifact (`source_observation_semantics_v1`) that the route A capture
for cutoff 2026-09-22 carries. The list itself is `REVIEWED_TEMPORAL_RULES` in
`engine/scripts/staging/produce_bridge_capture_inputs.py`; the consumer validator is
`parse_bridge_artifacts` in `engine/src/analysis/open_intelligence/source_estate_bridge_evidence.py`.

## What a rule says

A rule tells a reader whether it may treat a row's `published_at` as the time the thing
happened.

* `native_event_instant`: `published_at` is the item's own post or comment time, so a
  reader may say when it was posted.
* `collection_observation`: the row only shows that we saw the item when we collected it.
  A reader makes no claim about when it was posted.
* A missing or future time is never used as an event time (`withhold_event_time_claim`).

A rule does not decide which window an item counts in. The answer check refuses to cite an
item whose own `published_at` falls outside the question window, whatever its collection
time (`engine/src/analysis/open_intelligence/general_question_answer.py:452-457`).

## Scope

The rules key only the rows the funded Wave 1 SocialCrawl writer produces. They are not the
whole of what the capture carries.

* The capture copies the raw_content and enriched_content lanes whole, as of the collection
  snapshot instant, with no filter by source or endpoint (`source_estate_bridge.py:25`,
  `sp_consume_open_intelligence_source_snapshot_v3.sql:262-268`). This holds whichever
  dataset those lanes are read from.
* The funded run also runs every other connector except gdelt (`run_rss_now.py:173-176`,
  `run_rss_now.py:3348`), and earlier runs remain in the tables. RSS, the YouTube API and
  the others write the same two tables with an empty endpoint and source family, because
  only Wave 1 rows get those columns (`run_rss_now.py:286-293`). The validator requires
  both to be non-empty, so no rule can key those rows.
* A row no rule keys has no event time claim from these rules.
* The consumer, `parse_bridge_artifacts`, checks only the list's shape. No code yet matches
  a row to a rule, so a row without a rule is carried, not refused.

The Wave 1 keys come from the writer, not the channel map:

* routes: the eight frozen routes in `WAVE1_ROUTE_SPECS` (`socialcrawl.py:240`), the set
  the `wave1_pilot` authority binds by digest;
* endpoint: the route, written as is (`socialcrawl.py:938`, `raw_content.sql:32`);
* content type: platform plus the vendor envelope, post or comment (`socialcrawl.py:879`,
  `socialcrawl.py:917`). The writer takes either envelope on every route, so every route
  gets both keys;
* source family: the channel family of the route's platform (`candidates.py:293`,
  `candidates.py:388`, `raw_content.sql:35`).

## Time basis

The writer fills `published_at` from the item's own vendor field on every route
(`socialcrawl.py:925`). An absent or unreadable value becomes empty, never the collection
time (`socialcrawl.py:1096-1113`). The route catalog
(`engine/tests/fixtures/socialcrawl_catalog_2026_08_26.json`) says what kind of item each
route lists. A key is an event instant only when the route lists that kind of item: posts
on a post list route, comments on a comment list route. Every other key is left open by the
code and takes the conservative basis, collection observation. YouTube comment times take
it too: the writer accepts any ISO string (`socialcrawl.py:1096-1113`), so nothing shows
the vendor field is an exact instant rather than one derived from a relative age such as
"3 weeks ago".

| Source family | Endpoint | Content type | Basis | Why |
|:-|:-|:-|:-|:-|
| reddit | reddit/post/comments | reddit/comment | event instant | comment list; own time, `socialcrawl.py:925` |
| reddit | reddit/post/comments | reddit/post | collection observation | route lists comments; open |
| short_video | instagram/audio/reels | instagram/comment | collection observation | route lists reels; open |
| short_video | instagram/audio/reels | instagram/post | event instant | post list; own time, `socialcrawl.py:925` |
| short_video | instagram/music/trending | instagram/comment | collection observation | route lists music; open |
| short_video | instagram/music/trending | instagram/post | collection observation | route lists music, not posts; open |
| short_video | instagram/search/reels | instagram/comment | collection observation | route lists reels; open |
| short_video | instagram/search/reels | instagram/post | event instant | post list; own time, `socialcrawl.py:925` |
| short_video | tiktok/song | tiktok/comment | collection observation | route returns a song; open |
| short_video | tiktok/song | tiktok/post | collection observation | route returns a song, not a post; open |
| short_video | tiktok/song/videos | tiktok/comment | collection observation | route lists videos; open |
| short_video | tiktok/song/videos | tiktok/post | event instant | post list; own time, `socialcrawl.py:925` |
| youtube | youtube/shorts/trending | youtube/comment | collection observation | route lists shorts; open |
| youtube | youtube/shorts/trending | youtube/post | event instant | post list; own time, `socialcrawl.py:925` |
| youtube | youtube/video/comments | youtube/comment | collection observation | comment list, but exactness unshown, `socialcrawl.py:925`, `socialcrawl.py:1096-1113` |
| youtube | youtube/video/comments | youtube/post | collection observation | route lists comments; open |

The writer writes only post and comment rows, never period figures, so no key is a
metric interval.

## Not covered

* Rows of every connector other than the Wave 1 SocialCrawl writer, in both collection
  tables: no endpoint or source family, so no key and no rule (see Scope).
* Engine shaped rows: an item that already carries the engine row shape passes through
  with its own content type and family (`socialcrawl.py:875`). It is not a vendor post or
  comment envelope, and the list has no rule for its key.
