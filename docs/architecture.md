# 42 architecture

42 runs in one staging project on Google Cloud (ogilvy-trends-v2, region us-central1, BigQuery location US). Production is a separate step on Albert's written word.

## The pieces

| Piece | Where | What it does |
|---|---|---|
| Morning jobs | Cloud Run jobs from `core/collect`, `core/understand`, `core/detect`, `core/brief` | Collect, understand, detect and brief, chained on success (`core/collect/chain.py`). Each job refuses to start unless the one before it finished clean for the same day. |
| Support jobs | Cloud Run jobs from `core/collect`, `core/detect`, `core/setup`, `core/api` | GDELT seeds and daily aggregate, nightly credit reconcile, watchdog, weekly drift and learn, weekly calendar refresh, scheduled questions and the alert digest. The intraday pulse and Breaking are built and switched off. |
| Warehouse | BigQuery | `intelligence_42_core` holds posts, observations, raw responses, enrichment, items, states and the credit ledger. `intelligence_42_agent` holds runs, briefs, claim checks, findings, forecasts, watches and dossiers. Tables are appended to; nothing is deleted. |
| App and API | Cloud Run service f42-api (`core/api/app.py`) | Serves the React app in `app/frontend` and the read API behind the sign-in gate. |
| Ask | Cloud Run service f42-agent (`core/api/agent_app.py`, `core/agent`) | Ask, investigations, dossiers and findings, with live progress over server-sent events. |
| Models | Gemini on Vertex AI (`core/llm`) | gemini-3.8-flash for extraction, video reading, the brief and Ask; gemini-embedding-001 for embeddings through the BigQuery connection f42-vertex. Every call is priced and counted against MODEL_DAILY_USD. |
| Social data | SocialCrawl, through `core/collect/socialcrawl_client.py` only | Every call checks the credit ledger and the daily and monthly caps first, and every response is kept raw. |

## How data moves

1. Collect reads each market's feeds, boards, charts, panels and searches, appends every response to `raw_responses`, merges posts into `posts` and appends sightings to `post_observations`.
2. Understand embeds and enriches the day's posts, clusters them per market and reads selected clips.
3. Detect builds daily series, runs the tests against each item's own baseline, flags coordination, sets states and writes tomorrow's seeds.
4. Brief confirms the top candidates, writes one cited explanation per card, runs every card through the trust gate (`core/trust`) and publishes Today by 06:15 SAST.
5. The app reads Today, Discover and the rest from the warehouse. Ask reads the warehouse first and makes live searches only inside its per-question budget.

Identities, grants, caps and deploys are in [SETUP.md](full-42/SETUP.md) and the [operations guide](guide/operations.md).
