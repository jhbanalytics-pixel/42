# 42: the full specification

Version 1, 28 September 2026. Owner: Albert Meintjes, Ogilvy South Africa.

This is the single source of truth for building 42. Read it with ENGINE.md (the trend engine), TRUST.md (how 42 earns trust), BRAIN.md (how 42 thinks), SOURCES.md (where data comes from), AGENT.md (the analyst agent in detail), DATA.md (tables and detection maths), EXPERIENCE.md (screens), SETUP.md (cloud setup, permissions once), SALVAGE.md (what carries over) and BUILD.md (the order of work, with a check for every step).

## 1. What 42 is

42 is a cultural trend engine for strategists. Every morning it crawls every channel it can reach (TikTok, Instagram, YouTube, X, Facebook, Reddit, Threads, Bluesky, LinkedIn, Telegram, music and podcast charts, news, Wikipedia, calendars) in South Africa, Nigeria and Kenya, and any market added later, processes what it found and pulls out the key things. Its first job is to catch what is starting to trend, confirm it is real, explain it with posts a person can open, and have it in front of a strategist before the working day starts. Every output must be trustworthy first: nothing is published that the checks in TRUST.md have not passed. It is open: no fixed audience lens, no age groups, no pre-chosen view of culture. It finds what matters by itself (seeding), remembers every wave it has seen, and scores itself every week on how early and how right it was.

Asking questions, the morning brief, investigations, dossiers and client skins are ways of using what the engine finds. ENGINE.md is the heart of this specification.

The job it does for a strategist: "Show me what is taking off right now, why, and prove it."

## 2. Rules that never bend

Product rules (Albert, 28 September 2026):
1. Nothing about Gen Z. No age lens anywhere: not in seeding, scoring, prompts, copy or screens. The agent never infers age. Audience is described only by what posts show: language, place, interest, community, creator type, platform.
2. Google search interest comes only from SocialCrawl's google_trends/trending route, labelled as Google search data, and only to decide what to look into: never post evidence, never a count, a place, a quote or why-now proof, and never enough on its own for Today (RULES.md rule 2). Prism earliness and trend-board stay banned.
3. SocialCrawl is the main source. Everything else (GDELT, news feeds, Wikipedia, music and app charts, calendars) supports it.
4. Generated outputs from Gemini or Nano Banana are never evidence. Models are chosen per role by blind test on the 42 question set. Embedding models are not generative and may be used.
5. Every claim is tied to evidence someone can open. Every number is tied to the query that produced it. Gaps are said out loud.

Operating rules (Albert's standing rules):
6. Nothing goes to production without Albert's explicit written word for that step.
7. No data is deleted. No tables, datasets, buckets, secrets or branches are removed. Retention expiry counts as deletion and needs Albert's written approval.
8. IAM only adds. No owner or editor grants. No removing members.
9. Never print, echo, save or paste a secret value.
10. Commits are authored as Albert Meintjes <albert.meintjes@ogilvy.co.za>, with no tool or model names, trailers or generated-by lines anywhere in the repo or pull requests.
11. The builder never reviews its own work; a fresh reviewer does.

What is gone for good: approval tables and procedures, authority and admission layers, pinned manifests and digests (other than container images pinned automatically in CI), hand-written briefs for routine work, expiring policies, daily hand renewals, the 35-task certification programme.

## 3. The shape of the system

```
 SocialCrawl (all platforms)  GDELT  news RSS  Wikipedia  charts  calendars
      |                         |       |         |         |        |
      v                         v       v         v         v        v
 [1 Collect]  02:00 SAST: harvest, expand, deepen, remember;       (Cloud Run job)
              one credit ledger, hard caps
      |
      v
 [2 Understand] 04:00: extraction, embeddings, transcripts, video   (Cloud Run job)
                reading for clips that matter, clustering
      |
      v
 [3 Detect] 05:00: baselines, states, spread, novelty,              (SQL + job)
            authenticity, worth-attention, forecasts
      |
      v
 [4 Confirm and explain] 05:15: cross-platform confirmation;       (Cloud Run job)
            the brain writes cited explanations; the trust gate
            (TRUST.md) blocks anything unsupported
      |
      v
 [5 Use] 06:30 the app: Today (key things), Ask, Discover,        (Cloud Run, IAP)
         Compare, Investigations, Dossiers, History, Alerts, Coverage
      |
      v
 [6 Learn] weekly: detection scorecard, question score, trust      (weekly job)
           metrics, forecast scoring, seed yields
```

## 4. Decisions made (with the reason)

| Area | Decision | Why |
|---|---|---|
| Cloud | Staging stays in project ogilvy-trends-v2 (us-central1, BigQuery US) in new datasets intelligence_42_core and intelligence_42_agent. Production later in a new project Albert creates (suggested ogilvy-42-prod). | Reuse what exists; a separate production project keeps permissions simple and budgets exact. |
| Collection | Python Cloud Run jobs called by Cloud Scheduler and chained on success. Every SocialCrawl response is appended to raw_responses, then one MERGE on (platform, post_id) updates posts and an append writes post_observations. | Idempotent, nothing overwritten, and the raw response is always there to re-parse. dlt was dropped (its merge deletes and inserts, and it splits arrays into child tables). |
| Cadence | One morning run: collect 02:00, understand 04:00, detect 05:00, confirm and explain 05:15, publish 06:30 SAST. An intraday pulse is a later switch on the same code (ENGINE.md section 3). | The morning run already works as a process and needs no extra build; most cultural trends climb over days, and Ask covers same-day spikes with live calls. |
| Main source | SocialCrawl REST, funded key in secret SOCIALCRAWL_OGILVY_API_KEY. The zero-balance secret SOCIALCRAWL_API_KEY is never used. | Only funded key. |
| Credit and model caps | One caps table in SETUP.md: morning engine 2,400 credits a day (collect 2,000), Ask 600, month 80,000 with Ask throttled first, balance floor 20,000, and a daily model-spend cap. Checked in code before every call. | Hard stops in code replace approvals, and the morning run is protected. |
| Seeding | Daily earned-seed loop (SOURCES.md): harvest unseeded feeds, score, expand, deepen, remember; 10 to 15% exploration; diversity quotas. | Finds what is trending without a fixed view. |
| Agent framework | Python on Cloud Run for Ask (f42-agent): a research loop on Gemini function calling through Vertex AI, with tools limited to 42's own functions (core/agent/toolset.py); the morning brief is a fixed pipeline of structured model calls, not agent runs. | Researchers, a critic and budgets map onto the brain for open questions; a fixed pipeline is faster and more predictable for the 06:30 deadline. |
| Models | Gemini on Vertex AI for every role (Albert, 28 and 29 September: cost first): orchestrator, writer, researchers, critic, support checks, extraction and video reading run on GEMINI_MODEL (default gemini-3.8-flash on location global), with GEMINI_FAST_MODEL for the cheap roles when set; GEMINI_THINKING_LEVEL and GEMINI_THINKING_HEADROOM tune thinking, and list prices are built in (core/llm/provider.py). Calls run as the job or service identity. Embeddings gemini-embedding-001 at 768 dimensions. Roles are re-decided by blind test. Generated output is never evidence. | Strong reasoning where it matters, cheap where volume is high; honours the rule on Gemini outputs. |
| Clustering and trends | BERTopic with precomputed embeddings, daily merge into the cultural map; negative binomial and beta-binomial tests with Benjamini-Hochberg control (TRUST.md section 4); lifecycle rules in SQL, with states per ENGINE.md section 2. | Proven, open source, runs in the warehouse. |
| Memory | Findings and the cultural map in BigQuery with valid_from and valid_to on every fact (bi-temporal, Graphiti-style). | One store, queryable by the agent, keeps history of what 42 believed and when. |
| Verification | Stable evidence ids; quotes checked word for word by code; numbers pinned to run_id and result hash and re-run; code-computed confidence labels; a separate critic; a support checker that can only cut or downgrade. | Two independent checks on every answer. |
| Authenticity | Coordination detection in SQL (same object shared by many accounts inside a short window, near-duplicate text by embedding); flags shown, never silently dropped. | Botometer is gone; this method works on any platform. |
| Languages | Multilingual embeddings for search; model-based tone with the existing SSA slang lexicon; later a fine-tuned AfroXLMR model for sentiment in Zulu, Xhosa, Afrikaans, Pidgin, Yoruba, Hausa, Igbo and Swahili. | Local language is where 42 can beat global tools. |
| App | Keep the React app and the design system; point it at a new small API (FastAPI) with a live progress stream. | Screens and design are already built. |
| Access | Identity-Aware Proxy on Cloud Run for Ogilvy accounts; passcode kept only until IAP is on. | Per-person sign-in and audit. |
| Deploy | GitHub Actions with Workload Identity Federation: merge builds once, deploys to staging, smoke tests; production only through a manual workflow Albert alone can start. | No keys, no PC-only credentials, production on Albert's word. |
| Quality | 30-question set (core/eval/questions.yaml) and stored mornings, scored in replay mode from cached responses; production deploys need the candidate to score no lower than the last release on the same frozen snapshot, with citation integrity and number reproducibility at 100%. | A number that moves replaces certification, and a replay makes it comparable. |
| Build | One builder on Albert's PC, in the repo, on branch full-42, code in core/, with an independent reviewer and checks that enforce the rules above. | One owner of the code; rules enforced by checks, not by briefs. |

## 5. Every capability, and where it is specified

| Capability | Spec | Stage |
|---|---|---|
| Morning collection across all channels, seeding, credit ledger, balance check | ENGINE.md, SOURCES.md, DATA.md | 1 |
| Trust gate on every answer and every published trend | TRUST.md | 1 |
| Morning confirm and explain; Today with key things, held back, moments | ENGINE.md, EXPERIENCE.md | 1 |
| Moments calendar (next 90 days) | BUILD.md 1.4 | 1 |
| Evidence store with media, transcripts, creators | DATA.md | 1 |
| Ask with follow-ups, live research log, cited claims, four confidence labels | AGENT.md, EXPERIENCE.md | 1 |
| Live SocialCrawl calls inside answers, per-question budget | AGENT.md | 1 |
| Weekly quality score and forecast log | core/eval, DATA.md | 1 |
| Extraction, embeddings, clustering, cultural map | DATA.md | 2 |
| Trend detection: states, spread, novelty, authenticity, worth-attention | DATA.md, ENGINE.md | 2 |
| Detection scorecard: time to detect, lead time, alert precision | ENGINE.md | 2 |
| Discover: topics, sounds, formats, creators, hashtags | EXPERIENCE.md | 2 |
| Coverage and credits screen | EXPERIENCE.md | 2 |
| Alerts and watchlists (in-app, then email) | EXPERIENCE.md | 2 |
| Video reading on clips that matter | AGENT.md, DATA.md | 2 |
| Compare | EXPERIENCE.md | 3 |
| Creators, communities and language | EXPERIENCE.md | 3 |
| History: analogues, recurrence, past findings | BRAIN.md, DATA.md | 3 |
| Investigations (fieldwork) with editable plan | AGENT.md, EXPERIENCE.md | 3 |
| Dossiers: assemble, review, export HTML and PDF | EXPERIENCE.md | 3 |
| Moments calendar analogues from last year | SOURCES.md | 3 |
| Forecast scoring against persistence | DATA.md | 4 |
| Intraday pulse (optional switch) | ENGINE.md section 3 | 4 |
| Local-language sentiment model | BRAIN.md | 4 |
| Spike explanation ("why did this jump") | AGENT.md | 4 |
| Slack or Teams delivery, scheduled questions | EXPERIENCE.md | 4 |
| Brand lens: share of voice, competitor content, creator brand-fit | FEATURES.md | 5 |
| Client skins (saved lenses and report templates; BSA first) | ENGINE.md section 8, FEATURES.md | 5 |
| Production | SETUP.md | after stage 3, on Albert's word |

## 6. Definition of done for each stage

A stage is done when Albert can do the stage's "try this" list on staging himself and the checks in BUILD.md pass. Not before, and not by paperwork.
