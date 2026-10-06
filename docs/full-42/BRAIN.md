# The 42 brain

This is how 42 thinks. The engine (ENGINE.md) detects what is taking off every morning; the brain confirms it, explains it and answers any question about it. Collection, storage and the app exist to feed the brain and show its work, and TRUST.md decides what the brain may publish. When a design choice is unclear, choose the option that makes the brain's answers truer, more specific and easier to check.

## What the brain must be able to do

A strategist at Ogilvy asks 42 what a senior cultural analyst with a research team would be asked:

- What is happening in culture right now, in a market, a community, a category or a platform?
- What is new, what is rising, what is fading, and what only looks new because we have not seen it before?
- Why is it happening, who is driving it, and how is it spreading?
- Has this happened before, and what happened next?
- What does it mean for a brand, and what should the brand do or avoid?
- Is this real, or is it a handful of accounts, a bot push or paid content?

The brain answers with specific claims, each tied to evidence a person can open, with an honest confidence label, and it says plainly what it cannot know.

## The six layers

### 1. Perception: what is out there

Every post collected (daily pull or live call) is stored raw and then enriched once:

- Text: caption, title, body, top comments, transcript (SocialCrawl transcript endpoints first; speech to text only when missing).
- Visual: for the clips that matter (top by engagement or velocity in each cluster), a video model describes format, hook in the first three seconds, on-screen text, setting, people shown (never ages), products and brands visible, sound or song, edit style.
- Extraction by a fast, cheap model into fixed fields: language, place mentions, entities (brands, people, places, events, products), format, sound id, hashtags, tone, stance toward named entities, whether it looks like paid or sponsored content.
- Embedding of text plus transcript plus visual description, for semantic search and clustering.

Nothing about age groups. Community is described by what the posts show: language, place, interest, creator type, platform.

### 2. Memory: what 42 knows

Three kinds of memory, all queryable by the brain:

- Signal memory. Every post, creator, sound, hashtag and cluster, dated, in BigQuery. Nothing is overwritten; each day adds.
- Cultural map. A living table of the things culture is made of: topics, memes, sounds, formats, creators, communities, brands, events. For each: first seen, last seen, platforms, markets, lifecycle stage, related items, and the clusters that make it up. This is what lets 42 say "this is the third wave of this format since June".
- Findings memory. Every answer, brief, fieldwork pack and dossier 42 has produced, with its claims and cited posts. The brain searches its own past findings before starting fresh, and says when a new finding confirms or overturns an old one.

### 3. Detection: what is changing

Computed nightly, for every item in the cultural map and every cluster:

- Volume and engagement per day against its own 28-day baseline (ratio and z-score).
- Velocity and acceleration: is growth speeding up or slowing?
- Spread across platforms: where it appeared first and where it has moved (for example TikTok to X to news).
- Spread across creator tiers: niche creators to mid-tier to large accounts. Movement upward is an early mainstream signal.
- Spread across markets: Lagos to Nairobi to Johannesburg, and back.
- Novelty: a new cluster, or a recurrence of an old one (matched against the cultural map by embedding similarity).
- Lifecycle state, from ENGINE.md section 2: New to 42, Spike, On the boards, Emerging, Rising, Peaking, Mainstream, Fading, Recurring or Seasonal, with the rule that produced the label.
- Tone shift: stance toward a brand or topic changing week on week.
- Authenticity flags: many near-identical posts, new accounts, synchronised timing, paid-content markers. Flagged items are reported as flagged, not dropped silently.

Detection output feeds the daily brief and the alerts, and is a tool the brain can query.

### 4. Reasoning: how the brain answers

Every question runs the same loop. The loop is written as skills (markdown playbooks the agent loads), so strategists can improve the method without code changes.

1. Frame. Restate the question, name the market, time window and audience as asked. If the question is vague, choose a sensible reading, say which, and continue.
2. Recall. Search findings memory and the cultural map for what 42 already knows.
3. Plan. Break the question into sub-questions and choose sources: stored data first, then live SocialCrawl calls where stored data is thin, stale or missing a platform. Estimate credits before spending.
4. Gather. Run the SQL, semantic searches and live calls. Save every live result into signal memory so nothing is paid for twice.
5. Triangulate. A claim becomes a finding only when independent evidence supports it: at least two unrelated creators, and ideally two platforms. One source is labelled Single source.
6. Measure. Put numbers on each claim from the data (counts, growth against baseline, engagement, spread), never from the model's memory.
7. Contextualise. Compare with history (analogues from the cultural map), with the baseline, and with what is happening nearby. General world knowledge may frame a finding but is labelled as context, never as evidence.
8. Challenge. A separate critic pass, in a fresh context, tries to break each claim: checks the numbers against the cited posts, looks for counter-evidence, checks authenticity flags, checks whether the claim overreaches the evidence. Claims that fail are cut or downgraded.
9. Synthesise. Write the answer: the short answer, the findings with their evidence, what it means (tensions, opportunities, risks for brands), what to watch next, and what 42 does not know.
10. Record. Save the answer to findings memory, log any forward-looking call (for example "this will keep rising for two weeks") so learning can score it later.

### 5. Expression: how the brain shows its work

Every claim carries one of four labels:

- Observed: a direct count or fact from the data.
- Corroborated: supported by independent sources across creators or platforms.
- Single source: one source or a small cluster; worth watching, not yet a finding.
- Inferred: 42's reasoning from the evidence; the reasoning is stated.

Every claim links to its posts (platform, handle, date, link, engagement, quote or transcript line). Numbers link to the query that produced them. The same brain powers every surface: Ask, the daily brief, Discover, Compare, Creators, History, Fieldwork, dossiers and alerts.

### 6. Learning: how the brain gets better

- Weekly score on the 30 strategist questions, graded against a written rubric by a grader model plus spot checks by the team. The score and its trend are shown in the app.
- Every logged forward-looking call is checked after its horizon: did the item keep rising? 42 is scored against the simple baseline that "what rose last week keeps rising".
- Every human edit, thumbs down or reviewer rejection on a dossier is stored with its reason. Weekly, recurring failure patterns are written up as lessons in the skills, so the method improves in plain language anyone can read.
- The model behind each role (planner and writer, extractor, critic, video reader) is chosen by blind test on the 30 questions and re-tested when new models ship.

## Skills (the brain's playbooks)

Each is a markdown file in core/skills/, loaded by the agent when relevant:

- culture-read: the full ten-step loop above for an open question.
- trend-diagnosis: is this real, how big, what stage, who drives it, where is it going.
- spread-trace: first appearance, platform and market path, creator-tier path.
- brand-implication: what a trend means for a named brand or category, with tensions, opportunities and risks.
- compare: two or more topics, brands, markets or creators on the same measures.
- creator-read: who a creator is, their community, formats, recent posts, influence.
- analogue-search: past moments that looked like this and what followed.
- authenticity-check: bots, coordination and paid content.
- fieldwork: a larger, budgeted deep investigation that returns a findings pack.
- daily-brief: how to write the morning brief from detection output.

## Roles and models

- Analyst (plans, reasons, writes): the strongest model that wins the blind test.
- Critic (challenges): a strong model in a fresh context, ideally a different model from the analyst.
- Extractor (per-post fields): a fast, cheap model; volume is high.
- Video reader: a model that watches video; only for the clips that matter.
- Embeddings: a multilingual embedding model.

Gemini and image-generation outputs are never used as evidence. If a Gemini model wins a blind test for a role, it may do that role, with the result recorded.

## What makes this better than the old engine

The old engine hand-coded each step of evidence selection, admission and challenge in Python with pinned policies. The new brain keeps those ideas (independent evidence, challenge, quotation, honest gaps) but runs them as an agent loop with tools and written playbooks. It can go and get more evidence when it needs it, it reads video, it remembers what it found before, and its method is improved by editing plain language, measured weekly.
