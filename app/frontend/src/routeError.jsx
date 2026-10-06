/* A route whose code or render fails keeps the shell, the rail and every link
   working. After a deploy, a tab opened on the previous build asks for chunk
   names the new image no longer serves; the import then fails and, without
   this, React unmounts the whole app and leaves a blank page. A failed chunk
   reloads the page once, which fetches the current index and its chunks; any
   other failure, or a second chunk failure soon after, shows the error with
   Reload and Return to Today. Reloading only re-reads: no route starts paid
   work on load that it would not have started had the chunk arrived. */
import {Component} from 'react';
import {StateView} from 'ogilvy-intelligence-design-system';
import {routeStateView} from './instrumentRouteModels.js';

export const RELOAD_KEY = 'f42-chunk-reload-at';
const RELOAD_GAP_MS = 30000;

export function isChunkLoadError(error){
  const text = String((error && (error.message || error)) || '');
  return /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|Unable to preload CSS|ChunkLoadError/i.test(text);
}

function storage(){
  try { return typeof window !== 'undefined' ? window.sessionStorage : null; } catch { return null; }
}

/* True when this call reloaded the page; false when a reload ran too recently
   or storage is unavailable, so the caller shows the error instead of looping. */
export function reloadOnceForChunk(now = Date.now(), reload = () => window.location.reload()){
  const store = storage();
  if (!store) return false;
  let last = 0;
  try { last = Number(store.getItem(RELOAD_KEY)) || 0; } catch { return false; }
  if (now - last < RELOAD_GAP_MS) return false;
  try { store.setItem(RELOAD_KEY, String(now)); } catch { return false; }
  reload();
  return true;
}

export class RouteErrorBoundary extends Component {
  constructor(props){
    super(props);
    this.state = {error: null, reloading: false};
  }

  static getDerivedStateFromError(error){
    return {error};
  }

  componentDidCatch(error){
    if (isChunkLoadError(error) && reloadOnceForChunk(Date.now(), this.props.reload)) this.setState({reloading: true});
  }

  render(){
    const {error, reloading} = this.state;
    if (!error) return this.props.children;
    if (reloading) {
      return <div className="page"><StateView {...routeStateView({state: 'loading', title: 'Loading the latest version of 42', task: 'Reloading', body: 'A newer version of 42 is live, so this page is reloading.', reservedRows: 3})} /></div>;
    }
    const reload = this.props.reload || (() => window.location.reload());
    return (
      <div className="page"><StateView {...routeStateView({
        state: 'error',
        title: 'This page could not load',
        body: isChunkLoadError(error)
          ? 'Part of 42 did not arrive, usually because a newer version went live while this tab was open. Reload to open the latest version.'
          : 'Something on this page failed. Nothing you saved is lost. Reload, or go back to Today.',
        actions: [
          {id: 'route-reload', label: 'Reload', onClick: reload},
          {id: 'route-today', label: 'Return to Today', onClick: () => { if (window.location.hash.startsWith('#/pulse')) reload(); else window.location.hash = '#/pulse'; }},
        ],
      })} /></div>
    );
  }
}
