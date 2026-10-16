/* The figures a quantitative answer's sources state (core/agent/figures.py,
   carried in run.figures beside the answer). The lead line names the most
   recent source and its figures; then one bar chart per source, never two
   sources in one chart, each captioned with who ran it, when it was
   published, its sample and what it covers as the source states them, and
   each bar's exact quote one click away. Every figure was checked word for
   word against its post or article by code before it reached this page; 42
   computed none of them. */
import {BarList} from './ui/Charts42.jsx';
import {safeUrl} from './safeUrl.js';
import {platformLabel} from './ui/PlatformGlyph.jsx';

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
  'November', 'December'];

function longDay(value){
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  return m ? Number(m[3]) + ' ' + MONTHS[Number(m[2]) - 1] + ' ' + m[1] : '';
}

const text = (v) => (typeof v === 'string' ? v.trim() : '');
const finite = (v) => typeof v === 'number' && Number.isFinite(v);

function validTable(table){
  return table && typeof table === 'object' && Array.isArray(table.rows)
    && table.rows.some((r) => r && finite(r.value) && text(r.subject));
}

export function figuresOf(record){
  const payload = record?.run?.figures;
  if (!payload || typeof payload !== 'object') return null;
  const tables = Array.isArray(payload.tables) ? payload.tables.filter(validTable) : [];
  return {...payload, tables};
}

function caption(table){
  const parts = [];
  const by = text(table.source) || 'Pollster or source not stated';
  parts.push(by);
  const published = longDay(table.posted_at);
  if (published) parts.push('published ' + published + (text(table.publisher) ? ' by ' + text(table.publisher) : ''));
  parts.push(text(table.sample) ? 'sample ' + text(table.sample) : 'sample not stated');
  if (text(table.fieldwork)) parts.push('fieldwork ' + text(table.fieldwork));
  if (text(table.population)) parts.push('covers ' + text(table.population));
  return parts.join(' · ');
}

function FigureTable({table, index}){
  const rows = table.rows.filter((r) => r && finite(r.value) && text(r.subject)).map((r, i) => ({
    key: i + ':' + r.subject, label: r.subject, value: r.value, focus: i === 0 && index === 0,
  }));
  const link = safeUrl(table.url);
  const also = Array.isArray(table.also_reported_by) ? table.also_reported_by.filter((a) => a && text(a.publisher)) : [];
  return (
    <li className="ask42-figures-table" data-figure-source={text(table.source) || 'not stated'}>
      <BarList title={text(table.title) || 'Reported percentages'} caption={caption(table)} rows={rows} suffix="%" />
      {text(table.note) && <p className="ask42-figures-note">{table.note}</p>}
      <p className="ask42-figures-source">
        {link
          ? <a href={link} target="_blank" rel="noopener noreferrer">Open the source{table.platform ? ' (' + platformLabel(table.platform === 'twitter' ? 'x' : table.platform) + ')' : ''}</a>
          : <span>Source link unavailable</span>}
        {also.length > 0 && <span className="ask42-muted"> · Also reported by {also.map((a) => text(a.publisher)).join(', ')}</span>}
      </p>
      <details className="ask42-figures-quotes">
        <summary>The exact words each figure comes from</summary>
        <ul>
          {table.rows.filter((r) => r && text(r.quote)).map((r, i) => (
            <li key={i}><span className="ask42-figures-subject">{r.subject}</span> “{r.quote}”</li>
          ))}
        </ul>
      </details>
    </li>
  );
}

const NONE = {
  none_found: 'No source this answer read states a percentage for the question, so no figures are shown. A poll or survey the posts mention without its figures is not a figure.',
  failed: 'The figures the sources report could not be listed this time, so none are shown.',
  not_run: 'The question’s model budget had no room left to list the figures the sources report.',
};

export function ReportedFigures({record}){
  const figures = figuresOf(record);
  if (!figures) return null;
  if (!figures.tables.length){
    const words = NONE[figures.status] || NONE.none_found;
    return <section className="ask42-figures" aria-label="Reported figures"><p className="ask42-status">{words}</p></section>;
  }
  const swept = figures.sweep && finite(figures.sweep.matched) ? figures.sweep.matched : null;
  return (
    <section className="ask42-figures" aria-label="Reported figures">
      {text(figures.lead) && <p className="ask42-figures-lead">{figures.lead}</p>}
      <ol className="ask42-figures-list">
        {figures.tables.map((table, i) => <FigureTable key={i} table={table} index={i} />)}
      </ol>
      <p className="ask42-figures-basis ask42-muted">
        {text(figures.basis)}
        {swept !== null ? ' ' + swept + ' stored posts in the window that match the question and state a percentage were read for these.' : ''}
      </p>
    </section>
  );
}
