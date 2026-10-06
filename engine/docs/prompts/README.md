# PULSE Intelligence Next Level, prompt pack

This directory holds the runbook for the intelligence-next-level audit programme. It is docs-only work. No product code ships from this programme, no flags flip, no pipeline re-runs.

Files:

- `intelligence-next-level-RUN.md`: the execution spec, phases A through E with gates.
- `intelligence-next-level-notes-TEMPLATE.md`: blank notes skeleton. Copy to `docs/intelligence-next-level-notes.md` at the start of a run, never edit the template in place.

Process: work on branch `feat/intel-next-level` from origin/master. All findings land in the notes file as you go. GATE C and GATE D are hard stops for Albert. North stars are relevance and accuracy; a finding that improves neither is noise.
