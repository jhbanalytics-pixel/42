# 42 full redesign handoff

```text
Redesign directive: Redesign 42 Listening Post from purpose. Current staging failed the second design audit at 8/30, with critical gaps in usefulness, aesthetics, understandability, honesty, thoroughness, and restraint.

Verdict:
Current staging is a technically cleaner version of the old product, not the promised intelligence redesign. Accessibility and performance pass, but the information architecture, signal model, evidence model, copy, and visual grammar still encode the retired Brand24 and fixed-keyword world.

Why redesign and not refine:
The board identity, route structure, evidence hierarchy, audience claims, and repeated card grammar are wrong. Styling those structures again would preserve the failure.

Primary user:
An Ogilvy strategist who must detect what matters, understand why, inspect proof, compare options, and act.

Primary task:
Move from open cultural discovery to one evidence-backed decision without translating engine internals.

Preserve:
1. Editorial display type, provenance mono, dark and light themes, and semantic state colors.
2. Today, Explore, Compare, Build job navigation.
3. What changed, Why it matters, Possible response, Evidence.
4. Direct evidence links, cited brief workflow, focus behavior, reduced motion, accessibility, and performance gates.

Discard:
1. Fixed topic groups as board identity.
2. Fixed Try prompts and static glossary as current intelligence.
3. Universal rounded bordered card grammar.
4. Raw signal index, graph score, dataset names, and internal table names.
5. Duplicate rankings, duplicate retrieval routes, and duplicate status components.
6. Brand24 copy and unsupported age or demographic claims.
7. Decorative dots, gradients, glows, and theme controls without task value.
8. The visible Off, Calm, Balanced, and Lively motion selector. Honor the operating-system reduced-motion preference automatically and use one restrained default.

Priority moves:
1. Honest: dynamic discovered signals become the board identity. Taxonomy becomes optional tags. Audience labels state measured or inferred, source, window, and confidence.
2. Useful: Today becomes one decision path: what changed, why now, proof, possible response. Interpretation sits beside the signal.
3. Minimal: three visual grammars only, editorial lead, compact comparison row, and evidence drawer.
4. Understandable: novelty, breadth, source independence, history, evidence readiness, and audience fit replace engine internals.
5. Thorough: Source Lab, historical analogues, evidence readiness, source quality, and monthly credit use are visible.

Required product architecture:
1. Open discovery ingests broadly and forms dynamic clusters from phrases, hashtags, sounds, creators, entities, events, co-occurrence, embeddings, and cross-platform spread.
2. Fixed topic groups may label a discovery but may not decide what can exist.
3. Gen Z launches as an explicitly inferred lens. No age or gender claim is permitted until a measured demographic source is connected.
4. SocialCrawl Source Lab lists all 50 platforms and 400 endpoints with active, pilot, blocked, rejected, cost, yield, integrity, geo precision, last success, and downstream use.
5. Historical Intelligence uses the 1.46 million enriched rows, 1.03 million seed-graph rows, 18,693 events, 3,548 scored topic-days, and 2,684 briefs for analogues, replay, early warning, source allocation, and recurrence.
6. Evidence readiness states are ready, thin, contradictory, and unchecked. Recommendations cannot present as ready without supporting receipts.
7. Monthly SocialCrawl credits are budgeted by lane. No source is enabled because it exists. Each pilot has a credit cap and kill test.
8. An open question router accepts natural-language questions and produces a visible evidence plan containing intent, decision, markets, window, audience lenses, source families, historical comparison, evidence requirements, and output form. Supported intents include landscape, explanation, comparison, trajectory, audience, creator, brand role, whitespace, risk, historical analogue, campaign opportunity, source coverage, and custom.
9. Routing intents choose tools and evidence. They may not become another fixed question or keyword taxonomy.

One golden strategist acceptance task in a wider suite:
A strategist asks what local people are saying about an upcoming election, which expressed factors are associated with willingness to vote, and what role people expect brands to play. 42 asks only for missing scope such as market, election, and time window. It then produces one cited intelligence brief containing:
1. Scope, market, window, collection coverage, and limitations.
2. Local conversation themes with volume, movement, source independence, geo confidence, and representative receipts.
3. Expressed or associated decision drivers, never unsupported causal claims or polling claims.
4. Audience-lens differences, each labelled measured or inferred with confidence.
5. Expected brand roles, current brand behavior where observed, unmet spaces, and brand-safety risks.
6. Misinformation, polarization, or sensitive-political-content flags.
7. Evidence readiness, contradictions, missing sources, and what the tool cannot conclude.
8. Recommended content territories, do and do not guidance, and cited evidence.

The strategist must not choose an engine topic, graph score, table, or internal workflow to assemble this answer. The complete path is Ask, confirm scope, inspect evidence, export or build.

The golden-task suite must also cover:
1. What is newly emerging in one market without supplying a keyword.
2. Why a signal is moving and what evidence supports the explanation.
3. How the same cultural move differs across ZA, NG, and KE.
4. Which creators or communities carried a move first and who spread it.
5. What happened before, what is different now, and what usually follows.
6. What role a brand could credibly play and which spaces are already occupied.
7. Which audience lens fits the signal, with measured or inferred status.
8. Which source families agree, disagree, or remain absent.
9. What the engine is missing because a SocialCrawl surface is dark or underused.
10. A custom question that does not match a named intent, proving the router does not collapse into templates.

Visual constraints:
1. No more than eight text sizes. Component files contain no raw font-size values.
2. Spacing is limited to 4, 8, 12, 16, 24, 32, 48, and 64 pixels.
3. Radii are 12 or 18 pixels, pills only at 999 pixels. No more than three surface elevations.
4. Every signal summary contains signal, why now, proof, and possible response, or is a compact comparison row.
5. At 390 pixels, the first recommendation begins within 900 pixels. Every primary target is at least 48 pixels. Mobile retains one movement cue and one proof cue.
6. No idle animation, decorative glow, or background texture without a data purpose.
7. No appearance or motion control remains visible unless it changes a strategist task. Reduced motion is automatic.

Deliverables:
1. New information architecture, not derived from the current route wall.
2. Desktop and mobile wireframes for Today, Explore, Compare, Build, Source Lab, and Historical Intelligence.
3. Dynamic signal, evidence, audience lens, source capability, and credit-budget contracts.
4. A migration path from fixed topic groups to dynamic signals, including historical replay and rollback.
5. Empty, loading, error, success, focus, disabled, thin-evidence, contradictory-evidence, and no-discovery states.
6. Staging deployment batches, critique after each batch, and production cutover criteria.
7. A deterministic golden-task harness that runs the full open-question suite against fixtures, checks the required evidence plan and answer sections, rejects causal, demographic, predictive, and polling overclaims, verifies every citation, and renders each output on desktop and mobile.

Guardrails:
Do not port the old structure under new styling. Do not expose engine internals. Do not call categories trends. Do not claim demographics the data does not measure. Do not enable a SocialCrawl endpoint without a quality gate, cost cap, and kill test. Do not merge or deploy production without Albert's explicit sign-off.
```
