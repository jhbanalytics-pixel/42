# PULSE Intelligence Next Level, working notes

Run date:
Branch:
Auditor:

North stars: RELEVANCE (act before obvious) and ACCURACY (defend the number with receipts open). Failure modes: FP (false positive), FN (false negative), TR (trust failure).

## Phase A. Math and scoring QA

### A1. Score inventory (every score that reaches a human)

| Score | Formula / weights | Code location | BQ column | Consumer |
|---|---|---|---|---|

### A2. Recompute audit (>=15 topic-days, 5 per market, 7-day window)

| Market | Topic | Date | Stored | Recomputed | Delta | Flag >0.01 |
|---|---|---|---|---|---|---|

Accuracy watchdog run/mirror result:

### A3. Weight sensitivity

Dominant weight:
±20% shock findings:
Live-at-zero weights still displayed:

### A4. Corroboration honesty

Can social-only reach corroborated in live data? Proof:

### A5. Seed vs trend split

High-seed low-trend and inverse cases:
Does UI explain the split?

### GATE A verdict

## Phase B. Seeds autopsy

### B1. 14-day seed_candidates pull

Row counts, date coverage:

### B2. Grading (>=30 candidates)

| Candidate | Market | Date | Grade (USEFUL/OBVIOUS/NOISE/HARMFUL/UNCLEAR) | Why |
|---|---|---|---|---|

Grade distribution:

### B3. Failure tags

| Tag | Count | Examples |
|---|---|---|

### B4. Five worst, end-to-end trace through compute_c2_score

### B5. FN probe

Moves that mattered but not proposed; which gate killed each:

### B6. LP staging read (READ ONLY)

Does UI show breakdown or only terms?

### GATE B verdict

## Phase C. FP / FN / TR ranked

### C1. FP receipts

### C2. FN miss rate (ingested-not-ranked vs never-ingested)

### C3. TR findings (stale/future claims, reconcile action mix, fake precision, null-shaped numbers)

### C4. Ranked list (frequency x damage x fixability 2w/8w)

| Rank | Class | Finding | Frequency | Damage | Fixability | First fix class |
|---|---|---|---|---|---|---|

### C5. One-pager: what you are not seeing

### GATE C. STOP FOR ALBERT

## Phase D. Core + next level (after Albert go)

### D1. Core readiness matrix
### D2. Relevance law (ASSERT / RANK / PROPOSE)
### D3. Accuracy daily checks
### D4. Open possibilities (8-12), max 3 do-next
### D5. 30-day programme + non-goals
### D6. Direction doc written: docs/intelligence-next-level-direction.md

### GATE D. STOP FOR ALBERT

## Phase E. Handoff

### E1. Visual redesign handoff
### E2. Ordered fix prompt (only if Albert names one)
### E3. Deliverables checklist + max 5 open decisions
