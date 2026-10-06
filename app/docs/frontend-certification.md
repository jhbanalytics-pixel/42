# Frontend certification

This document states the frontend certification workload, the thresholds that were frozen before any measurement was taken, how the two browser suites take their measurements, where the proof output lives and how the candidate binding is filled at the native run. It covers the fixture slice: the suites run against the populated fixtures of the U05 state matrix served by the local preview build, and the proof output they write is bound to no candidate until the native staging run supplies the binding. The preview server sends its assets without cache validators, so the warm load in this slice re-downloads them from the loopback interface; the cache count on every warm row says so, and the native run measures the served headers the candidate actually sends.

## Frozen thresholds

The thresholds are the C07 proposals, frozen in `app/frontend/tests/browser/support/certification.mjs` as the `THRESHOLDS` constant. The shape test refuses a proof output whose thresholds differ from that constant, so a run cannot loosen a gate by editing its own output.

| Gate | Threshold | How it is measured |
|---|---|---|
| Largest contentful paint | at most 2500 ms | PerformanceObserver on `largest-contentful-paint`, buffered from before the first script, last candidate after the route settles |
| Cumulative layout shift | at most 0.1 | PerformanceObserver on `layout-shift`, largest session window without recent input, one second gap and five second cap; the plain sum is recorded beside it |
| Visible feedback | within 100 ms of the press | the pointer press stamps the start on the page clock; the first DOM mutation, the first frame on which the pressed control's painted style differs, or a hash navigation stamps the end |
| Horizontal page overflow | 0 px | `document.documentElement.scrollWidth` against `window.innerWidth` after fonts and animations settle |
| Action target | at least 44 by 44 CSS px on the 390 viewport | the border box of every button, summary, form control and anchor that is not a link inside a sentence |
| Contrast | at least 4.5 to 1 | text colour against the background actually painted under it in the theme, composited through translucent layers, for every rendered text run and for the primary action |
| Reduced motion | 0 moving elements | under the reduced motion preference, no declared animation and no transition on a property that moves or resizes a box |

Two further checks carry no numeric threshold. Every key panel the manifest names as the proof panel must be inside the viewport, or brought fully inside it by one scroll, and every rendered text range must sit inside its own container, measured with `getBoundingClientRect` against the nearest clipping ancestor or the marked panel. A text run inside a closed details element is laid out but not rendered and is not measured; its summary is.

Model answers are not measured by these suites. On the ask route the fixture holds `/api/chat/send`, so the send never answers. The suite records the held send as a separate model answer row marked `fixture, not a model answer` with a null latency and `counts_as_fast_useful_answer` false. The approved absolute deadline, including queue time, is bound at the native run, and a held or empty answer can never count as a fast useful answer whatever its timing.

## Workload

The workload is fixed by the same module and written into every proof output.

| Dimension | Values |
|---|---|
| Fixture state | populated, from `app/frontend/tests/browser/fixtures/42-capability-routes` |
| Capabilities | every manifest row whose producer is implemented: today_briefing, discovery_exploration, compare, sources_evidence, creators_network_language, questions_followups, fieldwork, coverage_source_lab, investigation_build_review |
| Request pressure routes | `#/pulse`, `#/explore`, `#/compare`, `#/console?work=ask` |
| Themes | daylight and midnight, selected through the stored `oi-theme` preference the app reads before its first paint |
| Viewports | 390x844, 768x1024, 1024x768, 1440x1000 |
| Flows | keyboard, reduced motion, cold load, warm load |
| Cache states | cold, the first load in a fresh browser context; warm, a reload of the same page in the same context, with the number of assets served from cache read from resource timing rather than assumed |
| Throttling | none, stated on every load row |
| Network | every `/api` path answered by the route interception fixture; a path outside the fixture fails the case |
| Fonts | bundled with the design package and awaited through `document.fonts.ready` |
| Browser | the system Chrome, headless, driven by the Playwright configuration under `app/frontend` |

The layout suite `42-layout-a11y.pw.mjs` runs one case per capability, theme and viewport, 72 cases, and one reduced motion case per capability and theme at 1024x768, 18 cases. The pressure suite `42-request-pressure.pw.mjs` runs one case per route, theme and viewport, 32 cases, each taking a cold and a warm load and one press. Assertions inside a case are soft, so one failing check never hides the others, and every check writes its measurement whether it passed or failed. A failed case keeps its full page screenshot under `app/frontend/test-results` and the proof output names the file.

## Proof output

Both suites record each measurement as an annotation on the test that took it. The reporter `app/frontend/tests/browser/support/certification-reporter.mjs`, registered in `playwright.config.mjs`, rebuilds the proof document at the end of a run from those annotations and writes it as JSON. Rows are keyed on suite, check, route, capability, theme, viewport, cache state and subject, then sorted, so two runs over the same candidate differ only in the values they measured. A case that ran replaces its own rows, keyed on suite, route, theme and viewport; a case that did not run keeps its rows from the previous document, so a partial rerun refreshes only what it measured.

Playwright creates one fresh disposable evidence directory under the operating system temporary directory for each run, then sets `CERTIFICATION_EVIDENCE_DIR` before workers start. The reporter writes `frontend-certification-<candidate>.json` there. An operator may supply an absolute `CERTIFICATION_EVIDENCE_DIR` to retain a run at a chosen protected location. The support module refuses missing or relative configuration. The candidate name is `unbound` until the native run sets `CERTIFICATION_CANDIDATE`.

| Section | Content |
|---|---|
| `candidate` and `candidate_binding` | the candidate name and its binding fields, all null with the reason `native binding pending` in this slice |
| `thresholds` | the frozen set above |
| `workload` | the table above as data |
| `measurements` | one row per suite, check, route, theme, viewport and, for loads, cache state: the value, its unit, the threshold and pass or fail, with a detail line naming the element, the offending run or the feedback kind |
| `model_answer_latency` | the held send rows, marked as fixture figures |
| `retained_screenshots` | the file kept for each failed case |
| `cases` | every case title with its status and duration |
| `visual_floor` | the ten Rams principles with null scores, the 24 of 30 floor and the minimum of 2 per principle, marked unscored |

The shape test `app/frontend/src/ui/__tests__/frontend-certification.test.js` validates a document built by the reporter's own builder, refuses mutated copies that drift on the binding, the thresholds, the workload, a measurement or a fixture answer, and validates the emitted file when it is present beside the checkout.

## The visual floor

The parent visual floor is scored by an independent rendered critique, not by these suites. The proof output carries the principle list unscored. A score may be written only when the scorer identity is present in a private file beside the proof output, `scorer-identity.private.json` by default or the path in `CERTIFICATION_SCORER_FILE`, which is never committed. The shape test refuses a scored floor without that file, refuses a partial score, refuses a total that is not the sum of its principles, and refuses a document that copies the identity into the output.

## Binding the candidate at the native run

At the native run the suites execute against the frozen staging candidate rather than the preview build, with `CERTIFICATION_CANDIDATE` naming the candidate. The binding fields are then filled from the deployment record, and every one of them must be present before the output counts as a certification receipt.

| Field | Source at the native run |
|---|---|
| `repo_commit` | the commit the candidate image was built from |
| `image_digest` | the container image digest serving the revision |
| `service_revision` | the Cloud Run revision name that served the capture |
| `served_asset_hashes` | the hashes of the assets the browser actually received, read from the served `index.html` and compared with the build |
| `design_package_version` | the vendored design package version the build pinned |
| `scope_digest` and `policy_digest` | the digests of the scope and policy documents in force for the candidate |
| `capture_identity` | the identity of the run that took the measurements, kept out of Git |
| `release_identity` | the identity of the release the candidate belongs to |

A later change to any bound field invalidates the affected receipts; the run is repeated against the new candidate and the previous output is kept beside it under its own candidate name. The reason field is null once the binding is complete. Until then the output is a fixture receipt and cannot replace a failing native check.
