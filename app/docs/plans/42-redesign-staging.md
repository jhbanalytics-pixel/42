# 42 staging redesign implementation plan

Base commit: `7cf7b97d674570ca58f4a27586ba895df8fedbcf`

Branch: `feat/42-redesign-staging`

Target service: `listening-post-staging` in `ogilvy-trends-v2`, `us-central1`

Production is outside this plan. Existing successful API shapes and saved hash routes remain valid.

## Batch 1: capacity control

Add one shared capacity gate for Chat, Ask brief generation, and research generation. A full queue returns HTTP 429, `Retry-After: 30`, and the approved `capacity_busy` payload. Capacity is released after both successful and failed jobs. Authentication and validation stay ahead of the capacity check.

Proof sequence:

1. Add focused endpoint tests for the full queue and release paths.
2. Run them and record the expected failures against the current unlimited job launchers.
3. Implement the smallest shared gate.
4. Run the focused tests, the API suites, and the complete Python suite.
5. Commit, push, deploy the exact commit to staging, and record the revision.

## Batch 2: navigation and overload recovery

Replace the crowded primary navigation with Today, Explore, Compare, and Build. Add one utility menu for Method, Network, Lexicon, My board, Browse, and Journey map. Preserve every existing route and alias. Mobile uses the same four jobs in a fixed bottom navigation. Remove the moving ticker and horizontal navigation scroller.

Chat, Ask, and research generation preserve the user's input when the capacity response occurs, show the approved message once, and expose Retry.

Proof sequence:

1. Add executable frontend contract tests that exercise route mapping, navigation semantics, utility access, and capacity error normalization.
2. Run them and record the expected failures.
3. Implement the shell and error recovery.
4. Run the frontend tests, production build, JSX check, contrast check, and accessibility scan.
5. Commit, push, deploy the exact commit to staging, and record the revision.

## Batch 3: Today and metric truth

Recompose the first viewport as product identity and actual freshness, one search and Ask surface, one lead signal, three rising signals, and three weakening signals. The lead signal contains What changed, Why it matters, Recommended next move, and Evidence. Move cumulative totals below the signal view as a compact Method summary and remove the duplicate Ask card.

Apply the approved labels and units: Current signal, Today's desk, updated from the latest run, How the last 14 days felt, Summed engagement, Channel mix, Signal index with `index, 0 to 1`, and Recommended next move. Every signal card states cited-post count and source-channel count. Confidence has a text label.

Proof sequence:

1. Add focused rendering and data-label tests, then record the expected failures.
2. Implement the minimal data-derived sections and responsive styles using existing tokens.
3. Run focused and complete local gates.
4. Commit, push, deploy the exact commit to staging, and record the revision.

## Batch 4: staging isolation and final proof

Confirm whether `listening-post-staging-cache` exists, create it only if absent, and deploy with that bucket. Label the revision with the exact `source-sha`. Keep the production bucket and service unchanged.

Run the complete staging matrix after prewarm: health and authentication, every retained route, Today, Explore, Compare, Build, topic, Listen, rich and thin Ask, capacity response, output labels, desktop and mobile accessibility, keyboard order, touch targets, desktop and mobile Lighthouse, LCP, CLS, ten cold topic probes, ten cold Listen probes, warm timings, security headers, client error states, and screenshot review. Record the staging revision, image digest, source label, cache bucket, and production service revision before and after.

Stop if a successful response shape must change, a route must be removed, production must be touched, or live evidence contradicts the approved contract.
