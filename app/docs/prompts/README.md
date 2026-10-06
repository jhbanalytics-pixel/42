# docs/prompts

Execution briefs for large design or engineering campaigns on the Listening Post. Each campaign gets one REFERENCE file (the full brief, the law for that campaign) and, where useful, a notes TEMPLATE the run copies into `docs/` and fills as it goes.

Rules for any session executing a brief from this directory:

1. Read the REFERENCE file in full before touching code. It overrides session defaults but never overrides the repository instructions, DATA_SPEC, merge flow, or push restrictions.
2. Copy the TEMPLATE (if one exists) to the destination named in the brief before Phase A. The notes file is the audit trail: skill outputs, screenshots, probe results, decisions.
3. A phase is not complete until its row in the notes skill log reads `Y` with the output pasted or linked.
4. Staging only. Production deploys go through the normal PR to main flow with Albert's explicit approval.

Current campaigns:

| File | Campaign |
|---|---|
| `v3-redesign-REFERENCE.md` | PULSE V3 full visual redesign of the Listening Post |
| `v3-redesign-notes-TEMPLATE.md` | Notes skeleton for the V3 redesign run |
