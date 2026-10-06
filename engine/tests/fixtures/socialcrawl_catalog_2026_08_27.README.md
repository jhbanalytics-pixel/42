# SocialCrawl catalog expectation fixture

This fixture freezes the 400 endpoint records returned by the authenticated, zero-credit `GET /v1/utility/endpoints` call reviewed on 27 August 2026.

It is a mechanical copy of the 26 August fixture with a sanitized dated request ID and exactly six approved endpoint field changes. `reddit/search` and `reddit/subreddit/search` each add `include_body`, change `metered` from false to true, and replace the flat 1 credit label with the reviewed metered 1 to 26 credit label.

The expected canonical route digest is `f843d6ad934e36b005090c1c642eb4b8e920bf2ca5d8828d7b9c1e7e4f143daa`. The expected normalized metadata digest is `47290bc1da76f4c29226a70c22f25b239557bbe56ce27d9931a29d174ffdd7ca`.

The 26 August fixture remains unchanged for historical replay. This fixture authorizes no paid call or pilot.
