/* Full-page line chart, inline SVG, ported from the prototype's bigChart.
   Auto-scales to the data's own range (no 0..1 clamp) so it serves both reach
   (millions) and share of voice (percent). Axis labels and the tail dot ride as
   positioned overlays so a non-uniform viewBox never distorts the text.
   Small row sparklines reuse the existing Sparkline in parts.jsx. */

import {useId} from 'react';

/* catmull-rom to bezier, the same smoothing the sparkline uses */
function smooth(pts){
  if (pts.length < 2) return '';
  let d = 'M' + pts[0][0].toFixed(2) + ' ' + pts[0][1].toFixed(2);
  for (let i = 0; i < pts.length - 1; i++){
    const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
    const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ' C' + c1x.toFixed(2) + ' ' + c1y.toFixed(2) + ' ' + c2x.toFixed(2) + ' ' + c2y.toFixed(2) + ' ' + p2[0].toFixed(2) + ' ' + p2[1].toFixed(2);
  }
  return d;
}

function shortDate(iso){
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleDateString('en-GB', {timeZone: 'UTC', day: 'numeric', month: 'short'});
}

/* readout: instrument-readout header rendered above the plot.
   {eyebrow, value, unit, delta, deltaColor, meta, tag} where value is the
   big serif current reading and tag is a ready-made pill node. In readout
   mode the numeric y-axis labels are dropped (the real numbers live in the
   header). Axis dates come from the supplied series. caption renders under the plot. */
export function BigChart({values, dates, format, color = 'var(--accent)', dashed = false, height = 240, label, readout, caption}){
  const instanceId = useId();
  if (!Array.isArray(values) || values.length < 2) return null;
  const fmt = typeof format === 'function' ? format : (v) => String(Math.round(v));
  const min = Math.min(...values), max = Math.max(...values);
  const spread = max - min;
  const lo = Math.max(0, min - spread * 0.2);
  let hi = max + spread * 0.18;
  if (hi <= lo) hi = lo + (Math.abs(max) || 1);
  const rng = (hi - lo) || 1;
  const n = values.length;
  const X = (i) => 1 + (i / (n - 1)) * 98;
  const Y = (v) => 94 - ((v - lo) / rng) * 88;
  const pts = values.map((v, i) => [X(i), Y(v)]);
  const line = smooth(pts);
  const area = line + ' L ' + X(n - 1).toFixed(2) + ' 100 L ' + X(0).toFixed(2) + ' 100 Z';
  const yt = [hi, (hi + lo) / 2, lo].map((v) => ({top: Y(v), label: fmt(v)}));
  const idxs = [...new Set([0, Math.round((n - 1) / 3), Math.round((n - 1) * 2 / 3), n - 1])];
  const xt = idxs.map((i) => ({left: X(i), label: shortDate(dates && dates[i])}));
  const dotX = X(n - 1), dotY = Y(values[n - 1]);
  const gid = 'bc' + instanceId.replace(/:/g, '');
  const titleId = gid + '-t';
  const descId = gid + '-d';
  const series = label || 'Signal';
  const alt = series + ' across ' + n + ' plotted observations, from ' + fmt(values[0]) + ' to a latest value of ' + fmt(values[n - 1]) + '.';

  return (
    <div className={'bigchart' + (readout ? ' bc-readout' : '')} style={{height: height + 'px'}}>
      {readout && (
        <div className="bc-ro-head">
          <div className="bc-ro-main">
            {readout.eyebrow && <div className="bc-ro-eyebrow">{readout.eyebrow}</div>}
            {readout.value != null && (
              <div className="bc-ro-valuerow">
                <span className="bc-ro-value">{readout.value}{readout.unit != null && <span className="bc-ro-unit">{readout.unit}</span>}</span>
                {readout.delta != null && <span className="bc-ro-delta" style={readout.deltaColor ? {color: readout.deltaColor} : undefined}>{readout.delta}</span>}
              </div>
            )}
            {readout.meta && <div className="bc-ro-meta">{readout.meta}</div>}
          </div>
          {readout.tag || null}
        </div>
      )}
      {!readout && label && <div className="bc-cap">{label}</div>}
      <div className="bc-plot">
        <svg className="bc-svg" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-labelledby={titleId + ' ' + descId}>
          <title id={titleId}>{series}</title>
          <desc id={descId}>{alt}</desc>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity="0.22" />
              <stop offset="100%" stopColor={color} stopOpacity="0" />
            </linearGradient>
          </defs>
          {yt.map((y, i) => <line key={i} x1="0" x2="100" y1={y.top} y2={y.top} className="bc-grid" />)}
          <path className="bc-area" d={area} fill={`url(#${gid})`} />
          <path className="bc-line" d={line} fill="none" stroke={color} strokeWidth="2.2"
            vectorEffect="non-scaling-stroke" strokeLinecap="round" strokeLinejoin="round"
            strokeDasharray={dashed ? '4 3' : undefined} />
        </svg>
        <span className="bc-dot" style={{left: dotX + '%', top: dotY + '%', background: color}} />
        {!readout && yt.map((y, i) => <span key={i} className="bc-yl" style={{top: y.top + '%'}}>{y.label}</span>)}
        {xt.map((x, i) => <span key={i} className={i === 0 ? 'bc-xl bc-xl--first' : 'bc-xl'} style={{left: x.left + '%'}}>{x.label}</span>)}
      </div>
      {caption && <div className="bc-caption">{caption}</div>}
    </div>
  );
}

export function MultiChart({series, dates, height = 230, label}){
  const instanceId = useId();
  const usable = (series || []).filter((s) => Array.isArray(s.values) && s.values.length > 1);
  if (!usable.length) return null;
  const all = usable.flatMap((s) => s.values);
  const min = Math.min(...all), max = Math.max(...all);
  const spread = max - min;
  const lo = Math.max(0, min - spread * 0.18);
  let hi = max + spread * 0.16;
  if (hi <= lo) hi = lo + (Math.abs(max) || 1);
  const rng = (hi - lo) || 1;
  const n = usable[0].values.length;
  const X = (i) => 1 + (i / (n - 1)) * 98;
  const Y = (v) => 94 - ((v - lo) / rng) * 88;
  const lines = usable.map((s) => {
    const pts = s.values.map((v, i) => [X(i), Y(v)]);
    return {label: s.label, color: s.color, line: smooth(pts), dotX: X(n - 1), dotY: Y(s.values[n - 1])};
  });
  const idxs = [...new Set([0, Math.round((n - 1) / 3), Math.round((n - 1) * 2 / 3), n - 1])];
  const xt = idxs.map((i) => ({left: X(i), label: shortDate(dates && dates[i])}));
  const mgid = 'mc' + instanceId.replace(/:/g, '');
  const titleId = mgid + '-t';
  const descId = mgid + '-d';
  const chartTitle = label || 'Signal comparison';
  const alt = 'Comparison across ' + n + ' plotted observations of ' + usable.map((s) => s.label + ' (latest ' + s.values[s.values.length - 1].toFixed(2) + ')').join(', ') + '.';

  return (
    <div className="bigchart" style={{height: height + 'px'}}>
      {label && (
        <div className="bc-cap mc-cap">
          <span>{label}</span>
          <span className="mc-legend">{usable.map((s, i) => <span className="mc-key" key={i}><i style={{background: s.color}} />{s.label}</span>)}</span>
        </div>
      )}
      <div className="bc-plot">
        <svg className="bc-svg" viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-labelledby={titleId + ' ' + descId}>
          <title id={titleId}>{chartTitle}</title>
          <desc id={descId}>{alt}</desc>
          {lines.map((l, i) => (
            <path key={i} className="bc-line" d={l.line} fill="none" stroke={l.color} strokeWidth="2.1"
              vectorEffect="non-scaling-stroke" strokeLinecap="round" strokeLinejoin="round" />
          ))}
        </svg>
        {lines.map((l, i) => <span key={i} className="bc-dot" style={{left: l.dotX + '%', top: l.dotY + '%', background: l.color}} />)}
        {xt.map((x, i) => <span key={i} className={i === 0 ? 'bc-xl bc-xl--first' : 'bc-xl'} style={{left: x.left + '%'}}>{x.label}</span>)}
      </div>
    </div>
  );
}
