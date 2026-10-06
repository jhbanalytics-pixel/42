import {useId} from 'react';
import {readerFigure} from '../api.js';
import './source-snapshot.css';

export function sourceSnapshotModel(payload){
  const catalog = payload?.catalog;
  const rows = payload?.sources;
  if (payload?.contract_version !== '2.2.0' || payload.resource_version !== 'source_lab_inventory_v2'
    || catalog?.completeness_state !== 'verified_snapshot'
    || typeof catalog.catalog_digest !== 'string' || !catalog.catalog_digest.trim()
    || typeof catalog.checked_at !== 'string' || !Number.isFinite(Date.parse(catalog.checked_at))
    || !Array.isArray(rows) || rows.length !== catalog.route_count
    || rows.some((row) => !row || typeof row.endpoint_id !== 'string' || !row.endpoint_id.trim())
    || new Set(rows.map((row) => row.endpoint_id)).size !== rows.length) return null;
  return {
    recordedAt: catalog.checked_at,
    documented: rows.length,
    active: rows.filter((row) => row.status === 'active').length,
    readings: rows.filter((row) => row.yield_state === 'measured' && Number.isSafeInteger(row.rows) && row.rows >= 0)
      .map((row) => ({
        id: row.endpoint_id,
        label: row.official_capability || row.route_path || row.platform,
        observations: row.rows,
        documentedCredits: typeof row.official_credits === 'number' && Number.isFinite(row.official_credits) && row.official_credits >= 0 ? row.official_credits : null,
      })),
  };
}

export function SourceSnapshot({payload}){
  const titleId = useId();
  const model = sourceSnapshotModel(payload);
  if (!model) return null;
  /* Quiet register, 23 Sept 2026: no eyebrow above the heading, and each
     count sits in its sentence as a figure with its word beside it, 1 204
     observations, rather than a large number over a small label. */
  return (
    <section className="source-snapshot" aria-labelledby={titleId}>
      <header className="source-snapshot__header">
        <h2 className="source-snapshot__title" id={titleId}>Recorded observations</h2>
        <p className="source-snapshot__scope">{readerFigure(model.active)} active {model.active === 1 ? 'route' : 'routes'} · {readerFigure(model.documented)} documented {model.documented === 1 ? 'route' : 'routes'}</p>
        <p className="source-snapshot__stamp">Snapshot recorded <time dateTime={model.recordedAt}>{new Date(model.recordedAt).toLocaleString('en-ZA', {timeZone: 'Africa/Johannesburg', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'})} SAST</time></p>
      </header>
      {model.readings.length ? model.readings.map((row) => (
        <details className="source-snapshot__row" key={row.id}>
          <summary className="source-snapshot__summary">
            <span className="source-snapshot__name">{row.label}</span>
            <span className="source-snapshot__value">{readerFigure(row.observations)} <span>{row.observations === 1 ? 'observation' : 'observations'}</span></span>
          </summary>
          <div className="source-snapshot__detail">
            <p>These are the route's observation counts carried into this snapshot. They are not a rate or a total of distinct observations across routes.</p>
            <p>Observation window was not supplied.</p>
            <p>Released signals from this route: Unmeasured. Observations are collected volume, not promoted signals.</p>
            {row.documentedCredits !== null ? <p>Documented price: {readerFigure(row.documentedCredits)} {row.documentedCredits === 1 ? 'credit' : 'credits'}. Catalogue prices do not measure spend.</p> : null}
          </div>
        </details>
      )) : <p className="source-snapshot__empty">This snapshot contains no measured observation counts.</p>}
    </section>
  );
}
