# SocialCrawl live probe, 23 July 2026

EnsembleData was cancelled and 20,000 SocialCrawl credits bought. This is the
evidence behind the surface choices in `configs/sources.yaml socialcrawl` and
`src/ingestion/connectors/socialcrawl.py`. Every line below came from a live
call against `https://www.socialcrawl.dev/v1` with the production key from
Secret Manager, not from the OpenAPI spec.

Read this before changing a parameter. Four of the surfaces return HTTP 200,
`success: true`, and zero rows when configured the way the docs imply.

## What the docs get wrong

| Surface | Docs say | Live behaviour | What we ship |
|---|---|---|---|
| `tiktok/search` | `date_posted` and `region` filters | **0 items with ANY value of either**, plain query returns 10 | not used |
| `tiktok/search/top` | `publish_time` | 30 items, all inside 7 days | **TikTok keyword surface** |
| `youtube/search` | `uploadDate` | `week` is a 400; the enum is `this_week` | `uploadDate=this_week` |
| `instagram/profile/posts` | `trim` shrinks the payload | `trim=true` returns 0 items; untrimmed returns 12 | untrimmed |
| `twitter/user/tweets` | returns tweets | 99 items, unsorted, `author.username` null, dates in Twitter format | parse + recency filter + handle fallback |
| `reddit/omni-search` | 1 credit | billed 5 | not used, `reddit/subreddit` + `reddit/search` cost 1 each |
| `google_trends/rising` | 5 credits | UPSTREAM_ERROR, timed out, refunded | not shipped |

## Envelopes

Three item shapes, not one.

```
post     data.items[].post      {author, content, engagement, flags, ext, published_at, url, id}
article  data.items[].article   {title, url, source, domain, snippet, published_at, rank}
author   data.author            {username, location, ...}   -- profile/region, not a list
```

Three timestamp formats, all seen in production data:

```
2026-07-23T12:55:16.000Z          most surfaces
2026-07-23 10:06:18 +00:00        google_news (DataForSEO)
Wed Oct 30 05:45:03 +0000 2019    twitter/user/tweets
```

The connector's `_parse_published` handles all three and returns `""` rather
than a garbage value when it cannot. An unparseable date is kept, not dropped:
some surfaces legitimately omit it.

## Geo scoping, the actual upgrade

EnsembleData had no geographic filter at all. TikTok's own API does not expose
one, which is why the engine carries a regex plus blocklist geo defence and
still leaked Nigerian and Kenyan collisions.

SocialCrawl has three real geo primitives, all live-probed on all three markets:

| Surface | Cost | ZA | NG | KE |
|---|---|---|---|---|
| `tiktok/trending?region=` | 5cr | 16 posts | 12 | 8 |
| `youtube/videos/trending?region=` | 1cr | 24 | 25 | 25 |
| `youtube/search?region=` | 1cr | 19 | 20 | 20 |
| `google_news/search?location_name=` | 1cr | 10 | - | - |

`tiktok/profile/region?handle=` returns `data.author.location = "ZA"`, a real
country for a real creator, for 1 credit. It is not wired into the connector
yet. It is the cheapest available fix for the geo-collision problem and should
be the next thing shipped: one call per watchlist handle, cached, would let the
engine label creator rows by verified country instead of inferring from slang.

`region` on `tiktok/search` and `tiktok/search/hashtag` is a proxy location, not
a filter, and the vendor documents it as such. It also returns zero rows on
`tiktok/search`, so it is not used.

## Recency

Hashtag and account surfaces happily return years-old content. Measured spread
of `published_at` per surface:

```
tiktok/search/hashtag    2025-05-02 .. 2026-07-22   mixed, stale-heavy
tiktok/search/top        2026-07-17 .. 2026-07-23   fresh, 7 days
youtube/search           2026-07-17 .. 2026-07-23   fresh
youtube/videos/trending  2026-07-09 .. 2026-07-22   fresh
tiktok/trending          2026-05-01 .. 2026-07-20   mixed
threads/search           2023-12-23 .. 2026-07-23   mixed, very stale tail
twitter/user/tweets      2019 .. 2026               unsorted, no recency at all
reddit/subreddit         2026-07-19 .. 2026-07-23   fresh
```

`max_age_days: 21` in config drops the tail. Without it a trends engine scores
2019 content as today's signal.

## Costs and refunds

Confirmed by watching `credits_used` on the wire:

- Cache hits bill **0**. A repeated identical call inside the cache window is
  free, which makes a same-day recovery re-run nearly free rather than a
  double-spend (the EnsembleData failure mode from 27 Jun).
- `RESOURCE_NOT_FOUND` (a handle that does not exist on that platform), upstream
  timeouts, and circuit-breaker rejections are **auto-refunded**, billed 0.
- `INSUFFICIENT_CREDITS` is what silently killed the eval channel on 8 Jul: the
  free tier's 100 credits were spent across 7 and 8 Jul, and every run since
  returned zero rows. The connector now sets a global halt flag on that error
  so it appears in `pipeline_runs.errors` instead of looking like an empty day.

## Method

`scripts` used for the probe are not committed; they were throwaway. The
sequence was: `socialcrawl_list_endpoints` per platform (0 credits) to get the
parameter names, the OpenAPI spec at `api-1.yaml` for the enums, then a live
call per surface capturing the envelope, item count, and date spread. Total
probe spend was under 40 credits.

Re-probe before trusting any of this after a vendor release. The spec gives the
path and the parameters; only the wire gives the envelope.
