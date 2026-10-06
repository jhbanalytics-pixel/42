/* Charts, 3 October 2026 (Albert: "stunning charts wherever possible, all
   types, data driven"). One small set of chart forms, each chosen for one
   job, shared by every page so 42 draws in one hand:

   BarList       magnitude across named things, ranked (a horizontal bar per row)
   PartsBar      part to whole (one bar split into labelled parts)
   StepMeter     progress through a known number of steps
   BulletBar     a measure against its cap
   Dumbbell      change between two moments, per row
   SmallMultiples one small area chart per series on a shared scale
   RangeTimeline spans on a shared time axis, with a peak marker
   UnitRows      how many of a group sit in each state (one square per unit)

   Rules every form keeps: only measured values are drawn; a missing value is
   said in words and never drawn as zero. Data is ink, Ogilvy red marks the
   one focal mark a page names. Every chart names its unit and window in its
   caption, labels its marks directly, and carries a visually hidden table
   with the same numbers for screen readers. Numbers keep their query id on
   the mark when the payload carries one. */
import {readerFigure} from '../api.js';
import {longDate} from './TrendCard.jsx';
import '../styles/charts42.css';

const num = (v) => typeof v === 'number' && Number.isFinite(v);
const fix = (n) => Number(n).toFixed(1);
const MONTHS3 = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
export function shortDay(date){
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(date || ''));
  const month = m ? MONTHS3[Number(m[2]) - 1] : null;
  return month ? Number(m[3]) + ' ' + month : '';
}

function Frame({title, caption, children, table, className = '', data}){
  return (
    <figure className={'ch42 ' + className} {...(data ? {[data]: ''} : {})}>
      {(title || caption) && (
        <figcaption className="ch42-head">
          {title && <span className="ch42-title">{title}</span>}
          {caption && <span className="ch42-caption">{caption}</span>}
        </figcaption>
      )}
      {children}
      {table}
    </figure>
  );
}

function HiddenTable({columns, rows}){
  return (
    <table className="sr-only">
      <thead><tr>{columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => <tr key={i}>{r.map((cell, k) => (k === 0 ? <th key={k} scope="row">{cell}</th> : <td key={k}>{cell}</td>))}</tr>)}</tbody>
    </table>
  );
}

const said = (v, unit) => (num(v) ? readerFigure(v) + (unit ? ' ' + unit : '') : 'not measured');

/* rows: [{key, label, value, note?, queryId?, focus?}]. Bars run from zero to
   the largest measured value; a row with no measure says so in place of a bar. */
export function BarList({title, caption, rows, unit = '', data}){
  const measured = rows.filter((r) => num(r.value));
  if (measured.length === 0) return null;
  const max = Math.max(...measured.map((r) => r.value), 1);
  return (
    <Frame title={title} caption={caption} className="ch42-barlist" data={data}
      table={<HiddenTable columns={['', unit || 'value']} rows={rows.map((r) => [r.label, said(r.value, unit)])} />}>
      <ol className="ch42-bars" aria-hidden="true">
        {rows.map((r, i) => (
          <li key={r.key || r.label} className={'ch42-bar-row' + (r.focus ? ' is-focus' : '')}>
            <span className="ch42-bar-label">{r.label}</span>
            {num(r.value)
              ? <span className="ch42-bar-track"><span className="ch42-bar-fill" style={{'--share': r.value / max, '--row': i}} {...(r.queryId ? {'data-query-id': r.queryId} : {})} /></span>
              : <span className="ch42-bar-none">Not measured</span>}
            <span className="ch42-bar-value">{num(r.value) ? readerFigure(r.value) : ''}{r.note ? <span className="ch42-bar-note"> {r.note}</span> : null}</span>
          </li>
        ))}
      </ol>
    </Frame>
  );
}

/* parts: [{key, label, value}] with value > 0; total defaults to their sum.
   One bar split into tones in the order given, each part labelled beneath
   with its count and share, so no reader has to match a colour to a key. */
export function PartsBar({title, caption, parts, total, data}){
  const shown = parts.filter((p) => num(p.value) && p.value > 0);
  const sum = num(total) && total > 0 ? total : shown.reduce((a, p) => a + p.value, 0);
  if (shown.length === 0 || sum <= 0) return null;
  /* Shares are rounded by largest remainder so the parts always add to 100%
     when they make up the whole. */
  const raw = shown.map((p) => (p.value / sum) * 100);
  const floors = raw.map(Math.floor);
  let left = Math.round(raw.reduce((a, v) => a + v, 0)) - floors.reduce((a, v) => a + v, 0);
  raw.map((v, i) => [v - floors[i], i]).sort((a, b) => b[0] - a[0]).forEach(([, i]) => { if (left > 0){ floors[i] += 1; left -= 1; } });
  const pcts = floors;
  return (
    <Frame title={title} caption={caption} className="ch42-parts" data={data}
      table={<HiddenTable columns={['', 'count', 'share']} rows={shown.map((p, i) => [p.label, readerFigure(p.value), pcts[i] + '%'])} />}>
      <div className="ch42-parts-bar" aria-hidden="true">
        {shown.map((p, i) => (
          <span key={p.key || p.label} className={'ch42-part ch42-tone-' + Math.min(i, 4)} style={{'--grow': p.value}} title={p.label + ': ' + readerFigure(p.value) + ' (' + pcts[i] + '%)'} />
        ))}
      </div>
      <ul className="ch42-parts-key" aria-hidden="true">
        {shown.map((p, i) => (
          <li key={p.key || p.label}>
            <span className={'ch42-swatch ch42-tone-' + Math.min(i, 4)} />
            <span className="ch42-key-label">{p.label}</span>
            <span className="ch42-key-value">{readerFigure(p.value)}<span className="ch42-key-share"> · {pcts[i]}%</span></span>
          </li>
        ))}
      </ul>
    </Frame>
  );
}

/* A count through a known number of steps: one cell per step, done in ink,
   the current step in red, the rest as outlines. */
export function StepMeter({title, caption, value, of, data}){
  if (!Number.isInteger(value) || !Number.isInteger(of) || of < 1 || of > 60) return null;
  const at = Math.max(0, Math.min(value, of));
  return (
    <Frame title={title} caption={caption} className="ch42-steps" data={data}
      table={<p className="sr-only">{at + ' of ' + of}</p>}>
      <span className="ch42-steps-row" aria-hidden="true">
        {Array.from({length: of}, (_, i) => (
          <span key={i} className={'ch42-step' + (i < at - 1 ? ' is-done' : i === at - 1 ? ' is-now' : '')} style={{'--row': i}} />
        ))}
      </span>
    </Frame>
  );
}

/* A measure against its cap: the bar is the measure, the tick is the cap. */
export function BulletBar({title, caption, value, cap, unit = '', format = readerFigure, data}){
  if (!num(value) || !num(cap) || cap <= 0) return null;
  const share = Math.min(1, value / cap);
  return (
    <Frame title={title} caption={caption} className="ch42-bullet" data={data}
      table={<p className="sr-only">{format(value) + ' of ' + format(cap) + (unit ? ' ' + unit : '')}</p>}>
      <span className="ch42-bullet-figure" aria-hidden="true">
        <span className="ch42-bullet-value">{format(value)}</span>
        <span className="ch42-bullet-of"> of {format(cap)}{unit ? ' ' + unit : ''}</span>
      </span>
      <span className="ch42-bullet-track" aria-hidden="true"><span className="ch42-bar-fill" style={{'--share': share}} /></span>
    </Frame>
  );
}

/* rows: [{key, label, from, to, focus?, queryId?}]. One line per row from the earlier value
   (hollow) to the later one (solid) on a shared scale; rising rows read left
   to right. A row missing either end says so. */
export function Dumbbell({title, caption, rows, fromLabel, toLabel, data}){
  const both = rows.filter((r) => num(r.from) && num(r.to));
  if (both.length === 0) return null;
  const max = Math.max(1, ...both.flatMap((r) => [r.from, r.to]));
  const W = 100;
  return (
    <Frame title={title} caption={caption} className="ch42-dumbbell" data={data}
      table={<HiddenTable columns={['', fromLabel, toLabel]} rows={rows.map((r) => [r.label, said(r.from), said(r.to)])} />}>
      <p className="ch42-legend" aria-hidden="true">
        <span><span className="ch42-dot-from" /> {fromLabel}</span>
        <span><span className="ch42-dot-to" /> {toLabel}</span>
      </p>
      <ol className="ch42-bars" aria-hidden="true">
        {rows.map((r) => {
          const ok = num(r.from) && num(r.to);
          const a = ok ? (r.from / max) * W : 0;
          const b = ok ? (r.to / max) * W : 0;
          return (
            <li key={r.key || r.label} className={'ch42-bar-row' + (r.focus ? ' is-focus' : '')}>
              <span className="ch42-bar-label">{r.label}</span>
              {ok
                ? <span className="ch42-db-track">
                    <span className="ch42-db-line" style={{left: Math.min(a, b) + '%', width: Math.abs(b - a) + '%'}} />
                    <span className="ch42-db-from" style={{left: a + '%'}} />
                    <span className={'ch42-db-to' + (r.to > r.from ? ' is-up' : '')} style={{left: b + '%'}} {...(r.queryId ? {'data-query-id': r.queryId} : {})} />
                  </span>
                : <span className="ch42-bar-none">Not measured</span>}
              <span className="ch42-bar-value">{ok ? readerFigure(r.from) + ' to ' + readerFigure(r.to) : ''}</span>
            </li>
          );
        })}
      </ol>
    </Frame>
  );
}

/* series: [{key, label, points: [{date, value}], queryId?}]. One small area chart per
   series, all on one y scale so their heights compare; a null value is a gap.
   The latest measured day carries the red point. */
export function SmallMultiples({title, caption, series, unit = '', data}){
  const usable = series.filter((s) => Array.isArray(s.points) && s.points.some((p) => num(p.value)));
  if (usable.length === 0) return null;
  const peak = Math.max(1, ...usable.flatMap((s) => s.points.map((p) => p.value).filter(num)));
  const W = 200, H = 56, pad = 2;
  return (
    <Frame title={title} caption={caption} className="ch42-multiples" data={data}
      table={<HiddenTable columns={['', 'first day', 'last day', 'peak']} rows={series.map((s) => {
        const pts = Array.isArray(s.points) ? s.points : [];
        const vals = pts.map((p) => p.value).filter(num);
        return vals.length === 0
          ? [s.label, '', '', 'not measured']
          : [s.label, shortDay(pts[0].date), shortDay(pts[pts.length - 1].date), readerFigure(Math.max(...vals)) + (unit ? ' ' + unit : '')];
      })} />}>
      <ul className="ch42-multiples-grid" aria-hidden="true">
        {usable.map((s) => {
          const n = s.points.length;
          const x = (i) => (n === 1 ? W / 2 : pad + (i * (W - 2 * pad)) / (n - 1));
          const y = (v) => H - pad - (v / peak) * (H - 2 * pad);
          let line = '';
          let area = '';
          let run = [];
          const flush = () => {
            if (run.length > 1){
              const top = run.map((i) => fix(x(i)) + ' ' + fix(y(s.points[i].value)));
              line += 'M ' + top.join(' L ') + ' ';
              area += 'M ' + top.join(' L ') + ' L ' + fix(x(run[run.length - 1])) + ' ' + (H - pad) + ' L ' + fix(x(run[0])) + ' ' + (H - pad) + ' Z ';
            }
            run = [];
          };
          s.points.forEach((p, i) => { if (num(p.value)) run.push(i); else flush(); });
          flush();
          const vals = s.points.map((p) => p.value).filter(num);
          const total = vals.reduce((a, v) => a + v, 0);
          const last = [...s.points].reverse().find((p) => num(p.value));
          return (
            <li key={s.key || s.label} className="ch42-multiple" {...(s.queryId ? {'data-query-id': s.queryId} : {})}>
              <span className="ch42-multiple-label">{s.label}</span>
              <span className="ch42-multiple-figure">{readerFigure(total)}<span className="ch42-multiple-unit"> {total === 1 ? unit.replace(/s$/, '') : unit}</span></span>
              <svg viewBox={'0 0 ' + W + ' ' + H} className="ch42-multiple-svg">
                <line className="ch42-axis" x1="0" x2={W} y1={H - pad} y2={H - pad} />
                {area && <path className="ch42-area" d={area} />}
                {line && <path className="ch42-line" d={line} pathLength="1" fill="none" />}
                {last && <circle className="ch42-now" cx={fix(x(s.points.lastIndexOf(last)))} cy={fix(y(last.value))} r="3" />}
              </svg>
              <span className="ch42-multiple-days"><span>{shortDay(s.points[0].date)}</span><span>{last ? shortDay(last.date) : ''}</span></span>
            </li>
          );
        })}
      </ul>
    </Frame>
  );
}

/* rows: [{key, label, start, peak, end, peakValue, focus?, queryId?}] with ISO dates; end
   may be null for a wave still running. Each row is a span on one shared time
   axis: the rise to the peak in a lighter tone, the fade after it in ink, and
   a mark at the peak sized by nothing (its value is written beside it). */
export function RangeTimeline({title, caption, rows, unit = '', data}){
  const t = (d) => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(d || '')); return m ? Date.UTC(+m[1], +m[2] - 1, +m[3]) : null; };
  const usable = rows.filter((r) => t(r.start) !== null && t(r.peak) !== null);
  if (usable.length === 0) return null;
  const lo = Math.min(...usable.map((r) => t(r.start)));
  const hi = Math.max(...usable.map((r) => t(r.end) ?? t(r.peak)));
  const span = Math.max(1, hi - lo);
  const at = (d) => ((t(d) - lo) / span) * 100;
  const first = usable.reduce((a, r) => (t(r.start) < t(a.start) ? r : a)).start;
  const lastDay = usable.reduce((a, r) => ((t(r.end) ?? t(r.peak)) > (t(a.end) ?? t(a.peak)) ? r : a));
  return (
    <Frame title={title} caption={caption} className="ch42-timeline" data={data}
      table={<HiddenTable columns={['', 'start', 'peak', 'end', 'peak ' + unit]} rows={usable.map((r) => [r.label, longDate(r.start), longDate(r.peak), r.end ? longDate(r.end) : 'still running', said(r.peakValue)])} />}>
      <ol className="ch42-bars" aria-hidden="true">
        {usable.map((r) => {
          const s = at(r.start), p = at(r.peak), e = r.end ? at(r.end) : p;
          return (
            <li key={r.key || r.label} className={'ch42-bar-row' + (r.focus ? ' is-focus' : '')}>
              <span className="ch42-bar-label">{r.label}</span>
              <span className="ch42-tl-track">
                <span className="ch42-tl-rise" style={{left: s + '%', width: Math.max(0.5, p - s) + '%'}} />
                {r.end ? <span className="ch42-tl-fade" style={{left: p + '%', width: Math.max(0.5, e - p) + '%'}} /> : null}
                <span className="ch42-tl-peak" style={{left: p + '%'}} {...(r.queryId ? {'data-query-id': r.queryId} : {})} />
              </span>
              <span className="ch42-bar-value">{num(r.peakValue) ? readerFigure(r.peakValue) + (unit ? ' ' + unit : '') : ''}</span>
            </li>
          );
        })}
      </ol>
      <p className="ch42-axis-days" aria-hidden="true"><span>{shortDay(first)}</span><span>{shortDay(lastDay.end || lastDay.peak)}</span></p>
    </Frame>
  );
}

/* rows: [{key, label, counts: {state: n}}]; states: [{key, label, tone}] in
   the order to draw, tone one of "ink", "soft", "alert", "open". One square
   per unit, so a row reads as "12 sources, 3 failed" at a glance; open states
   (nothing recorded, off) are outlines, never filled. Every state is named
   with its count beside the row, so colour is never the only key. */
export function UnitRows({title, caption, rows, states, unit = '', data}){
  const usable = rows.filter((r) => r.counts && states.some((st) => Number.isInteger(r.counts[st.key]) && r.counts[st.key] > 0));
  if (usable.length === 0) return null;
  return (
    <Frame title={title} caption={caption} className="ch42-units" data={data}
      table={<HiddenTable columns={['', ...states.map((st) => st.label)]} rows={usable.map((r) => [r.label, ...states.map((st) => String(r.counts[st.key] || 0))])} />}>
      <ul className="ch42-unit-rows" aria-hidden="true">
        {usable.map((r) => {
          const shown = states.filter((st) => Number.isInteger(r.counts[st.key]) && r.counts[st.key] > 0);
          const total = shown.reduce((a, st) => a + r.counts[st.key], 0);
          return (
            <li key={r.key || r.label} className="ch42-unit-row">
              <span className="ch42-bar-label">{r.label}<span className="ch42-key-share"> · {readerFigure(total)}{unit ? ' ' + (total === 1 ? unit.replace(/s$/, '') : unit) : ''}</span></span>
              <span className="ch42-unit-cells">
                {shown.flatMap((st) => Array.from({length: Math.min(r.counts[st.key], 200)}, (_, i) => <span key={st.key + i} className={'ch42-unit ch42-unit-' + st.tone} />))}
              </span>
              <span className="ch42-unit-key">
                {shown.map((st) => <span key={st.key}><span className={'ch42-unit ch42-unit-' + st.tone} /> {readerFigure(r.counts[st.key])} {st.label}</span>)}
              </span>
            </li>
          );
        })}
      </ul>
    </Frame>
  );
}
