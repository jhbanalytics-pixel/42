# SocialCrawl catalog expectation fixture

This fixture freezes the 400 endpoint records returned by the authenticated, zero-credit `GET /v1/utility/endpoints` call retained on 27 August 2026 under the Source Lab expectation approved on 26 August 2026.

The fixture removes the request receipt and live balance because both are runtime observations. It preserves every endpoint field used by canonical normalization. The expected canonical route digest is `f843d6ad934e36b005090c1c642eb4b8e920bf2ca5d8828d7b9c1e7e4f143daa`. The expected normalized metadata digest is `9432a98874dfeafa020211c9aee11d81cbb0270620a8e4988af89033c4bd61a0`.

Any route or metadata change requires a new reviewed expectation version. This fixture authorizes no paid call or pilot.
