# 42 staging QA checklist

Run against the staging URL from `docs/staging-deploy.md` before prod flip.

| Check | Pass criteria |
|---|---|
| Staging URL loads | Console workbench renders, passcode gate works |
| Passcode test | Wrong passcode rejected; correct passcode opens Console |
| Behaviour scan enabled | Step 1 "See the behaviours first" appears on `#/console?work=brief` |
| Client metrics default | Header shows `N posts · M comments · engagement`; no trend score until "Engine detail" toggled |
| Comment examples | At least one ZA topic with ig_post_comments data shows a `comment ·` example row (handle-only OK, no URL required) |
| Post examples | Post rows still show platform + link when URL present |
| Approve flow | Approve 1+ behaviours, Continue to brief still works |
| API direct | `GET /api/research/behaviours?markets=za` returns `comment_count` in metric block |
| 42 phase R3 consolidated doc | Approve 2+ behaviours, click **Build consolidated doc**, wait for synthesis; doc shows per-behaviour sections and cross-cutting block when N>=2 |
| 42 phase R3 API | `POST /api/research/generate-consolidated` with approved behaviour rows returns 202 then `consolidated: true` on status poll |
| 42 phase R5 evidence pack | After consolidated or batch brief completes, **Evidence pack link** copies a URL; opening `/api/research/{artifact_id}/evidence-pack.html` shows Posts and Comments subsections per behaviour |
| 42 phase R5 passcode | Evidence pack URL requires `X-Passcode` header (or open while logged into Console on same origin) |

## Blockers for prod flip

| Blocker | Owner |
|---|---|
| Staging checklist not signed | Albert |
| Thapelo sign-off on six deck topics | T, after 42 phase R3 staging pass |

The 42 phases R1, R2, R2b, R3 and R5 staging path is sufficient for Console Research QA. Prod flip still needs staging sign-off and Thapelo deck review.

## Log results

Append a row to `docs/qa_log.md` after each staging run:

`| YYYY-MM-DD | STAGING 42 | url=… | pass/fail | notes |`
