import {useEffect, useState} from 'react';

import {FAILURE_ORIGIN_LABEL, apiPost, failureCode, failureOrigin} from './api.js';
import {workspaceGapLabel} from './model.js';
import {go} from './router.js';
import {NextActions} from './ui/index.js';
import './styles/workspaces.css';

/* Quiet register, 23 Sept 2026: the title says what the page is, so no
   eyebrow sits over it. The mode the reader opened, analogue, recurrence or
   replay, is said in the lead under the title rather than as a label above
   it, and a failed read offers the way back to Build beside the re-read. */
export function HistoricalWorkspaceView({state, data, mode, error, onRetry}){
  if (state === 'landing') return <section className="workspace-page"><h1>Historical</h1><p>Open an investigation from Build to inspect approved analogue, recurrence or replay evidence.</p><button type="button" onClick={() => go('/console?work=brief')}>Back to Build</button><NextActions actions={[{route: '/fieldwork', label: 'Open Fieldwork'}, {route: '/source-lab', label: 'Open Source Lab'}, {route: '/explore', label: 'Open Discover'}]} /></section>;
  if (state === 'loading') return <section className="workspace-page workspace-loading" aria-busy="true"><h1>Historical</h1><p>Loading {mode} evidence.</p><div className="workspace-skeleton" /></section>;
  if (state !== 'ready') return <section className="workspace-page workspace-error" role="alert"><h1>Historical</h1><p>Historical evidence is unavailable for this investigation.</p>{error && <details className="workspace-reason"><summary>Details</summary><p>{FAILURE_ORIGIN_LABEL[error.origin || failureOrigin(error.code || error)]} <code>{error.code || error}</code></p></details>}<p className="workspace-actions">{onRetry && <button type="button" onClick={onRetry}>Try again</button>}<button type="button" onClick={() => go('/console?work=brief')}>Back to Build</button></p></section>;
  const items = Array.isArray(data && data.items) ? data.items : [];
  return <section className="workspace-page historical-workspace"><header className="workspace-lead"><h1>Historical</h1><span className="workspace-state">{workspaceGapLabel(data.state)}</span>{mode ? <p>{workspaceGapLabel(mode)} evidence for this investigation.</p> : null}</header>{items.length ? <ol className="workspace-rows">{items.map((item) => <li key={item.item_id} className="workspace-row"><strong>{item.claim_text}</strong><p>{item.relationship || 'Relationship unavailable'}</p><p>{(item.limitations || []).join(' \u00b7 ')}</p></li>)}</ol> : <p>No eligible Historical evidence is available. Return to the Evidence Room plan.</p>}</section>;
}

export function HistoricalWorkspace({route, onAuth}){
  const invalid = route && route.error;
  const investigationId = route && route.investigationId;
  const mode = route && route.mode;
  /* A route this page refused is its own finding, so it is announced as
     derived. A failed read carries the code the read recorded: the body's own
     where it sent one, the status where it did not, and network_unreachable
     where no answer arrived. */
  const [view, setView] = useState(() => invalid ? {state: 'error', error: {code: invalid, origin: 'derived'}} : investigationId ? {state: 'loading'} : {state: 'landing'});
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (invalid) { setView({state: 'error', error: {code: invalid, origin: 'derived'}}); return; }
    if (!investigationId) { setView({state: 'landing'}); return; }
    let live = true;
    setView({state: 'loading'});
    apiPost(`/api/internal/v2/investigations/${encodeURIComponent(investigationId)}/historical/read`, {mode})
      .then((data) => { if (live) setView({state: 'ready', data}); })
      .catch((error) => { if (!live) return; if (error && error.auth) onAuth && onAuth(); else setView({state: 'error', error: {code: failureCode(error)}}); });
    return () => { live = false; };
  }, [invalid, investigationId, mode, retry, onAuth]);
  return <HistoricalWorkspaceView {...view} mode={mode} onRetry={() => setRetry((value) => value + 1)} />;
}
