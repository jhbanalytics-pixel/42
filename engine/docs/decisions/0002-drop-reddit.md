# ADR 0002: Drop Reddit from V2 connector set

**Date:** 2026-04-14
**Author:** Albert Meintjes
**Status:** Superseded (Reddit is live via EnsembleData, see note below)

> **Superseded.** Reddit was later added to the live connector set through
> EnsembleData (posts + top comments + slang keyword search), sharing the
> Ensemble unit ledger. See the connector table in README.md. The original
> decision and its reasoning are kept below as the record.

## Decision

Reddit is not in the V2 connector set. The MVP included a `reddit_connector.py` (69 lines). It will not be ported.

## Context

The Trends Engine targets emerging cultural trends among young audiences in Nigeria, Kenya, and South Africa. Reddit's demographic in these markets is negligible. It skews toward Western, English-speaking, tech-adjacent users. Penetration in NG and KE is minimal by any measure.

Reddit also changed its API pricing in 2023 to block most third-party access. The free tier limits make meaningful data collection impractical for a production pipeline running three times daily across three markets.

## Consequences

The V2 connector set is: EnsembleData (TikTok, Instagram, Threads), YouTube, GDELT (news), RSS (web), and BigQuery Trends (search intent). Five sources covering the social platforms with genuine SSA youth penetration.

Reddit can be reconsidered post-launch if market data shows meaningful signal density in any of the three markets.
