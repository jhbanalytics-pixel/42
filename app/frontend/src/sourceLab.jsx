import {useEffect, useState} from 'react';

import {apiGetFresh, readerFigure, sentenceCase} from './api.js';
import {catalogueLabel, fundingAccountLabel, laneLabel, platformLabel, routeStatusText, snapshotTime, stateText} from './plainLabels.js';
import {SourceSnapshot} from './ui/SourceSnapshot.jsx';
import './styles/workspaces.css';

/* The inventory pages twenty five rows at a time behind a Show more control,
   as Fieldwork does, so a 500 row catalog lands as one screen rather than a
   hundred thousand pixel page. The control is a button, never a route. */
const INVENTORY_PAGE = 25;

function verifiedSnapshot(data){
  /* Verification and emptiness are different questions. A snapshot verifies
     when the catalog is complete and consistent with its list; whether that
     list has anything in it is a fact about the estate, not about the read.
     A catalog claiming routes while the list is empty is inconsistent and
     does not verify. */
  return !!(data && data.contract_version === '2.2.0'
    && data.resource_version === 'source_lab_inventory_v2'
    && data.catalog && data.catalog.completeness_state === 'verified_snapshot'
    && Array.isArray(data.sources)
    && data.sources.length === data.catalog.route_count
    && data.credit_budget && data.credit_budget.contract_version === 'credit_budget_v2'
    && Array.isArray(data.credit_budget.lanes)
    && data.credit_budget.lanes.length === 2);
}

function measured(value){
  return value == null ? 'Unknown' : readerFigure(value);
}

/* A lane figure nothing counts reads Unmeasured; a counted one is written the
   South African way, 25 000 rather than 25000. */
function laneFigure(value){
  return value == null ? 'Unmeasured' : readerFigure(value);
}


function CreditBudget({reading}){
  return (
    <section className="workspace-budget" aria-labelledby="source-lab-budget" data-budget-state={reading.state}>
      <h2 id="source-lab-budget">Credit budget</h2>
      <p className="workspace-limitation">{reading.limitation}</p>
      <ol className="workspace-rows">
        {reading.lanes.map((lane) => (
          <li className="workspace-row" key={lane.credential_lane} data-lane-state={lane.attribution_state}>
            <div>
              <strong>{laneLabel(lane.credential_lane)}</strong>
              <span>{fundingAccountLabel(lane.funding_account)}</span>
              {/* A lane that repeats the budget's own limitation word for word
                  says it once, above the lanes, rather than twice. */}
              {lane.limitation && lane.limitation !== reading.limitation ? <p>{lane.limitation}</p> : null}
              <details><summary>Lane reference</summary><dl><div><dt>Credential lane</dt><dd><code>{lane.credential_lane}</code></dd></div><div><dt>Funding account</dt><dd><code>{lane.funding_account}</code></dd></div></dl></details>
            </div>
            <dl>
              <div><dt>Monthly cap</dt><dd>{laneFigure(lane.monthly_cap)}</dd></div>
              <div><dt>Effective spend</dt><dd>{laneFigure(lane.monthly_effective_spend)}</dd></div>
              <div><dt>Remaining</dt><dd>{laneFigure(lane.monthly_remaining)}</dd></div>
              <div><dt>Balance</dt><dd>{laneFigure(lane.current_balance)}</dd></div>
              <div><dt>Run allowance</dt><dd>{laneFigure(lane.run_allowance)}</dd></div>
              <div><dt>Attribution</dt><dd>{sentenceCase(stateText(lane.attribution_state))}</dd></div>
              <div><dt>Kill state</dt><dd>{sentenceCase(stateText(lane.kill_state))}</dd></div>
            </dl>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function SourceLabView({state, data, error, onRetry}){
  const [visibleCount, setVisibleCount] = useState(INVENTORY_PAGE);
  /* The band is gone rather than retuned a second time. Task 65 cut it from
     2,600 pixels to the shared 280 and round 18 still measured the page
     standing 570.36 loading against 340.36 settled at 1280, and 554 against
     324 at 390, so it went on collapsing by 230 at both widths. A loading
     branch may reserve only what every settle of its route shares, and what
     the three settles of this route share is the title and one sentence. They
     are what this reserves; the page grows once from here rather than moving
     twice. The band stays declared for Historical and the Evidence Room,
     whose settles hold against it. */
  if (state === 'loading') return <section className="workspace-page workspace-loading" aria-busy="true"><h1>Source Lab</h1><p>Loading Source Lab inventory and funding controls.</p></section>;
  if (state !== 'ready' || !verifiedSnapshot(data)) return <section className="workspace-page workspace-error" role="alert"><h1>Source Lab</h1><p>Source Lab could not verify a complete snapshot.</p>{error && <details className="workspace-reason"><summary>Details</summary><code>{error}</code></details>}{onRetry && <button type="button" onClick={onRetry}>Retry</button>}</section>;
  if (data.sources.length === 0) return <section className="workspace-page source-lab" data-workspace-state="empty">
    <header className="workspace-lead"><h1>Source Lab</h1><span className="workspace-state">Verified snapshot</span></header>
    {/* Empty said plainly, only after the snapshot verified. */}
    <p>No sources are documented in this catalogue yet.</p>
  </section>;
  return <section className="workspace-page source-lab" data-workspace-state="ready">
    {/* Quiet register, 23 Sept 2026: the page title says what the page is, so
        the eyebrow above it went. The lead is the catalogue in words and the
        time it was checked in SAST; the digest a reader would quote and the
        stamp as the producer sent it wait under Catalogue reference, in mono. */}
    <header className="workspace-lead"><h1>Source Lab</h1><span className="workspace-state">Verified snapshot</span><p>{catalogueLabel(data.catalog.expectation_version)}{' \u00b7 '}{readerFigure(data.catalog.route_count)} documented routes{' \u00b7 '}checked {snapshotTime(data.catalog.checked_at)}</p><details className="workspace-reference"><summary>Catalogue reference</summary><p>{data.catalog.expectation_version}</p><p>{String(data.catalog.catalog_digest)}</p><p>{data.catalog.checked_at}</p></details></header>
    <SourceSnapshot payload={data} />
    {data.credit_budget && <CreditBudget reading={data.credit_budget} />}
    <section className="workspace-funding" aria-labelledby="source-lab-funding"><h2 id="source-lab-funding">Funding control</h2><dl><div><dt>Balance</dt><dd>{measured(data.funding.balance)}</dd></div><div><dt>Recent deductions</dt><dd>{measured(data.funding.recent_deductions)}</dd></div><div><dt>Funding state</dt><dd>{sentenceCase(stateText(data.funding.funding_math_status))}</dd></div><div><dt>Optional calls</dt><dd>{data.funding.optional_calls_enabled ? 'Enabled' : 'Disabled'}</dd></div></dl></section>
    {/* Each source leads with its platform and what it does. The route path is
        a code a reader copies, so it sits in the evidence with the other codes,
        in mono, rather than under the name. */}
    <section aria-labelledby="source-lab-sources"><h2 id="source-lab-sources">Documented sources</h2><p className="workspace-limitation">Catalogue availability is not activation. Unmeasured values remain Unknown, not zero.</p><ol className="workspace-rows">{data.sources.slice(0, visibleCount).map((source) => <li key={source.endpoint_id} className="workspace-row"><div><strong>{platformLabel(source.platform)}</strong><p>{source.official_capability}</p></div><details><summary>Open evidence</summary><dl><div><dt>Route</dt><dd><code>{source.route_path}</code></dd></div><div><dt>Documented credits</dt><dd>{measured(source.official_credits)}</dd></div><div><dt>Parameters</dt><dd>{(source.official_parameters || []).length ? <code>{source.official_parameters.join(', ')}</code> : 'None documented'}</dd></div><div><dt>Cache</dt><dd>{source.cache_ttl_seconds == null ? 'Unknown' : readerFigure(source.cache_ttl_seconds) + ' seconds'}</dd></div><div><dt>Measured calls</dt><dd>{measured(source.calls)}</dd></div><div><dt>Route status</dt><dd>{sentenceCase(routeStatusText(source.status))}</dd></div><div><dt>Downstream use</dt><dd>{(source.downstream_consumers || []).length ? <code>{source.downstream_consumers.join(', ')}</code> : 'None recorded'}</dd></div></dl><a href={source.docs_url}>Documentation</a></details></li>)}</ol>{visibleCount < data.sources.length ? <p className="workspace-more"><button type="button" onClick={() => setVisibleCount((count) => count + INVENTORY_PAGE)}>Show more</button><span>{readerFigure(Math.min(visibleCount, data.sources.length))} of {readerFigure(data.sources.length)} documented sources shown.</span></p> : null}</section>
  </section>;
}

export function SourceLabWorkspace({onAuth}){
  const [view, setView] = useState({state: 'loading'});
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let live = true;
    setView({state: 'loading'});
    loadSourceLab()
      .then((data) => { if (live) setView({state: 'ready', data}); })
      .catch((error) => { if (!live) return; if (error && error.auth) onAuth && onAuth(); else setView({state: 'error', error: error && (error.code ? error.code + ': ' + error.message : error.message)}); });
    return () => { live = false; };
  }, [retry, onAuth]);
  return <SourceLabView {...view} onRetry={() => setRetry((value) => value + 1)} />;
}

export function loadSourceLab(get = apiGetFresh){
  return get('/api/v2/source-lab?contract_version=2.2.0');
}
