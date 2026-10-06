/* PULSE · shared parts: icons, sparkline, big chart, momentum, sentiment, count-up */
import {useState, useEffect, useRef} from 'react';
import {sentimentLabel, GLOSSARY} from './model.js';
import './styles/gate.css';

function useChartConstructor(active){
  const [constructor, setConstructor] = useState(null);
  useEffect(() => {
    if (!active || constructor) return undefined;
    let live = true;
    import('chart.js/auto').then((module) => {
      if (live) setConstructor(() => module.default);
    });
    return () => { live = false; };
  }, [active, constructor]);
  return constructor;
}

/* ==== responsive: true at or below the phone breakpoint, live-updating so a
   resize reflows without a reload. Drives mobile layouts in the inline-styled
   views where a CSS media query cannot reach the inline grid. ==== */
export function useIsMobile(maxWidth = 640){
  const q = '(max-width: ' + maxWidth + 'px)';
  const [m, setM] = useState(() => typeof window !== 'undefined' && window.matchMedia(q).matches);
  useEffect(() => {
    const mq = window.matchMedia(q);
    const on = () => setM(mq.matches);
    on();
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, [q]);
  return m;
}

/* ==== dialog a11y: focus management + Tab trap for the slide-in panels ====
   Pass the panel ref and an open flag. On open it stores the trigger, moves
   focus into the panel (the close button, else the panel), traps Tab inside
   while open, and on close restores focus to the trigger. Escape is left to the
   host so it can run its own teardown (abort polls, clear state). ==== */
export function useDialog(ref, open){
  const trigger = useRef(null);
  useEffect(() => {
    const node = ref.current;
    if (!open || !node) return;
    trigger.current = document.activeElement;
    const focusables = () => Array.from(node.querySelectorAll(
      'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])',
    )).filter((el) => el.offsetParent !== null || el === document.activeElement);
    const first = node.querySelector('.panel-close') || focusables()[0] || node;
    requestAnimationFrame(() => { try { first.focus({preventScroll: true}); } catch (e){ first.focus(); } });
    const onKey = (e) => {
      if (e.key !== 'Tab') return;
      const items = focusables();
      if (!items.length){ e.preventDefault(); return; }
      const a = items[0], z = items[items.length - 1];
      if (e.shiftKey && document.activeElement === a){ e.preventDefault(); z.focus(); }
      else if (!e.shiftKey && document.activeElement === z){ e.preventDefault(); a.focus(); }
    };
    node.addEventListener('keydown', onKey);
    return () => {
      node.removeEventListener('keydown', onKey);
      const t = trigger.current;
      if (t && typeof t.focus === 'function'){ try { t.focus({preventScroll: true}); } catch (e){ t.focus(); } }
    };
  }, [open]);
}

/* ==== icons ==== */
export const Icon = {
  arrUp: (p) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M7 17 17 7M9 7h8v8"/></svg>,
  arrRight: (p) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M5 12h14M13 6l6 6-6 6"/></svg>,
  arrFlat: (p) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M4 12h16"/></svg>,
  arrDown: (p) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M7 7 17 17M17 9v8H9"/></svg>,
  search: (p) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" {...p}><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>,
  close: (p) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" {...p}><path d="M6 6l12 12M18 6 6 18"/></svg>,
  image: (p) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.5"/><path d="m4 17 5-5 4 4 3-3 4 4"/></svg>,
  music: (p) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/></svg>,
  sun: (p) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6 19 19M19 5l-1.4 1.4M6.4 17.6 5 19"/></svg>,
  moon: (p) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>,
  star: (p) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="m12 3.5 2.6 5.4 5.9.8-4.3 4.1 1 5.9L12 16.9l-5.2 2.8 1-5.9-4.3-4.1 5.9-.8z"/></svg>,
};

/* ==== glossary: dotted-underline local terms with hover definitions ==== */
const GLOSS_TERMS = Object.keys(GLOSSARY);
const GLOSS_RE = GLOSS_TERMS.length
  ? new RegExp('\\b(' + GLOSS_TERMS.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')\\b', 'gi')
  : null;

export function Gloss({text}){
  if (!text || !GLOSS_RE) return text || null;
  const parts = String(text).split(GLOSS_RE);
  return parts.map((p, i) => {
    const def = GLOSSARY[p.toLowerCase()];
    return def ? <span key={i} className="gloss" title={def}>{p}</span> : p;
  });
}

export const MOM_META = {
  rising:   {label: 'Rising',   icon: Icon.arrUp,    color: 'var(--rising)'},
  building: {label: 'Building', icon: Icon.arrRight, color: 'var(--building)'},
  steady:   {label: 'Steady',   icon: Icon.arrFlat,  color: 'var(--steady)'},
  cooling:  {label: 'Cooling',  icon: Icon.arrDown,  color: 'var(--cooling)'},
};

/* Wave 1 lifecycle phase: where a trend sits in its arc. Colour tracks the
   momentum scale (growth reads like rising, decline like cooling). Only these
   four values render; anything else no-ops. */
export const LIFECYCLE_META = {
  birth:    {label: 'Birth',    color: 'var(--building)'},
  growth:   {label: 'Growth',   color: 'var(--rising)'},
  maturity: {label: 'Maturity', color: 'var(--steady)'},
  decline:  {label: 'Decline',  color: 'var(--cooling)'},
};

/* Wave 1 continuity: how many consecutive days the trend has held the board,
   and whether it just came back after a gap. Only these four values render. */
export const CONTINUITY_META = {
  new:        {label: 'New',         color: 'var(--rising)'},
  day2:       {label: 'Day 2',       color: 'var(--building)'},
  day3plus:   {label: 'Day 3+',      color: 'var(--steady)'},
  rebounding: {label: 'Rebounding',  color: 'var(--building)'},
};

export function MomentumTag({m}){
  const meta = MOM_META[m] || MOM_META.steady;
  const I = meta.icon;
  return <span className={'mom ' + m}><I className="arr" /> {meta.label}</span>;
}

/* ==== catmull-rom to bezier smoothing for sparkline paths ==== */
function smoothPath(pts){
  let d = 'M' + pts[0][0].toFixed(1) + ' ' + pts[0][1].toFixed(1);
  for (let i = 0; i < pts.length - 1; i++){
    const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
    const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ' C' + c1x.toFixed(1) + ' ' + c1y.toFixed(1) + ' ' + c2x.toFixed(1) + ' ' + c2y.toFixed(1) + ' ' + p2[0].toFixed(1) + ' ' + p2[1].toFixed(1);
  }
  return d;
}

/* ==== sparkline (always drawn; draw-in is an additive overlay animation) ==== */
export function Sparkline({data, color, height = 46, area = true, animate = true, dot = true, strokeWidth = 2}){
  const ref = useRef(null);
  const W = 300, H = height;
  if (!Array.isArray(data) || data.length < 2) return null;
  const min = Math.min(...data), max = Math.max(...data);
  const rng = (max - min) || 1;
  const pad = 4;
  const pts = data.map((v, i) => {
    const x = (i / (data.length - 1)) * (W - pad * 2) + pad;
    const y = H - pad - ((v - min) / rng) * (H - pad * 2);
    return [x, y];
  });
  const line = smoothPath(pts);
  const areaD = line + ` L ${pts[pts.length - 1][0].toFixed(1)} ${H} L ${pts[0][0].toFixed(1)} ${H} Z`;
  const last = pts[pts.length - 1];
  const gid = 'g' + Math.abs((data[0] * 997 + data[data.length - 1] * 131 + height) | 0) + (color || '').replace(/\W/g, '');

  useEffect(() => {
    if (!animate || !ref.current) return;
    const path = ref.current;
    let len = 0;
    try { len = path.getTotalLength(); } catch (e){ return; }
    path.style.strokeDasharray = len;
    path.style.strokeDashoffset = '0';
    if (path.animate){
      try {
        path.animate([{strokeDashoffset: len}, {strokeDashoffset: 0}], {duration: 1100, easing: 'cubic-bezier(0.16,1,0.3,1)', fill: 'none'});
      } catch (e){ /* optional */ }
    }
  }, [line, animate]);

  return (
    <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{display: 'block', overflow: 'visible'}}>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.3" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {area && <path className="spark-area" d={areaD} fill={`url(#${gid})`} />}
      <path ref={ref} className="spark-path" d={line} vectorEffect="non-scaling-stroke" style={{stroke: color, strokeWidth}} />
      {dot && <>
        <circle className="spark-dot-halo" cx={last[0]} cy={last[1]} r="6" style={{fill: color}} />
        <circle className="spark-dot" cx={last[0]} cy={last[1]} r="2.6" style={{fill: color}} />
      </>}
    </svg>
  );
}

/* ==== chart color plumbing: resolve CSS vars to concrete colors ==== */
function resolveColor(cssColor){
  const probe = document.createElement('span');
  probe.style.display = 'none';
  probe.style.color = cssColor;
  document.body.appendChild(probe);
  const out = getComputedStyle(probe).color;
  document.body.removeChild(probe);
  return out;
}
const chartAlpha = (c, a) => {
  if (c.startsWith('rgba(')) return c.replace(/[\d.]+\)\s*$/, a + ')');
  if (c.startsWith('rgb(')) return c.replace('rgb(', 'rgba(').replace(')', ', ' + a + ')');
  /* oklch() / oklab() / color(): strip any existing alpha, then append "/ a" */
  return c.replace(/\s*\/\s*[\d.%]+\)\s*$/, ')').replace(/\)\s*$/, ' / ' + a + ')');
};
/* Matches the loaded face in tokens.css --mono; JetBrains Mono is never shipped. */
const CHART_MONO = "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace";

/* charts rebuild when the theme flips so their colors follow the tokens */
function useDir(){
  const [dir, setDir] = useState(() => document.documentElement.getAttribute('data-dir'));
  useEffect(() => {
    const mo = new MutationObserver(() => setDir(document.documentElement.getAttribute('data-dir')));
    mo.observe(document.documentElement, {attributes: true, attributeFilter: ['data-dir']});
    return () => mo.disconnect();
  }, []);
  return dir;
}

function dateLabels(n){
  return Array.from({length: n}, (_, i) => {
    const d = new Date(Date.now() - (n - 1 - i) * 864e5);
    return i === n - 1 ? 'TODAY' : d.toLocaleDateString('en-GB', {day: 'numeric', month: 'short'});
  });
}

function crosshairPlugin(id, faint){
  return {
    id: id,
    afterDatasetsDraw(ch){
      const act = ch.tooltip && ch.tooltip.getActiveElements && ch.tooltip.getActiveElements();
      if (!act || !act.length) return;
      const x = act[0].element.x;
      const c = ch.ctx;
      c.save();
      c.setLineDash([3, 4]);
      c.strokeStyle = chartAlpha(faint, 0.8);
      c.lineWidth = 1;
      c.beginPath();
      c.moveTo(x, ch.chartArea.top);
      c.lineTo(x, ch.chartArea.bottom);
      c.stroke();
      c.restore();
    }
  };
}

/* ==== big terminal chart: smooth curve, tooltips, crosshair, real dates ====
   events: [{i, label}] indices into data draw dashed vertical markers with a
   mono uppercase label (the client-side ACTIVATED marker rides this). */
export function MomentumChart({data, color, height = 240, events}){
  const canvasRef = useRef(null);
  const chartRef = useRef(null);
  const dir = useDir();
  const eventsSig = JSON.stringify(events || []);
  const ChartConstructor = useChartConstructor(Array.isArray(data) && data.length >= 2);

  useEffect(() => {
    if (!ChartConstructor || !canvasRef.current || !Array.isArray(data) || data.length < 2) return;
    const lineC = resolveColor(color);
    const faint = resolveColor('var(--faint)');
    const gridC = resolveColor('var(--line)');
    const tipBg = resolveColor('var(--surface-2)');
    const inkC = resolveColor('var(--ink)');
    const mutedC = resolveColor('var(--muted)');

    const eventMarks = {
      id: 'pulseEvents',
      afterDatasetsDraw(ch){
        (events || []).forEach((ev) => {
          if (!ev || typeof ev.i !== 'number') return;
          const meta = ch.getDatasetMeta(0);
          const el = meta.data[ev.i];
          if (!el) return;
          const c = ch.ctx;
          c.save();
          c.setLineDash([2, 3]);
          c.strokeStyle = chartAlpha(mutedC, 0.65);
          c.lineWidth = 1;
          c.beginPath();
          c.moveTo(el.x, ch.chartArea.top + 12);
          c.lineTo(el.x, ch.chartArea.bottom);
          c.stroke();
          c.setLineDash([]);
          c.font = '600 8.5px JetBrains Mono';
          c.fillStyle = mutedC;
          const flip = el.x > ch.chartArea.right - 80;
          c.textAlign = flip ? 'right' : 'left';
          c.fillText(String(ev.label || '').toUpperCase(), el.x + (flip ? -4 : 4), ch.chartArea.top + 9);
          c.restore();
        });
      }
    };

    const labels = dateLabels(data.length);
    const min = Math.min(...data), max = Math.max(...data);
    const lo = Math.max(0, min - (max - min) * 0.18), hi = Math.min(1, max + (max - min) * 0.16);

    const ctx = canvasRef.current.getContext('2d');
    const g = ctx.createLinearGradient(0, 0, 0, height);
    g.addColorStop(0, chartAlpha(lineC, 0.24));
    g.addColorStop(1, chartAlpha(lineC, 0));

    if (chartRef.current) chartRef.current.destroy();
    chartRef.current = new ChartConstructor(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data,
          borderColor: lineC,
          borderWidth: 2.4,
          tension: 0.4,
          cubicInterpolationMode: 'monotone',
          fill: true,
          backgroundColor: g,
          pointRadius: (c) => c.dataIndex === data.length - 1 ? 3.5 : 0,
          pointHoverRadius: 4.5,
          pointBackgroundColor: lineC,
          pointBorderColor: lineC,
          pointHoverBackgroundColor: lineC,
          pointHoverBorderColor: chartAlpha(lineC, 0.35),
          pointHoverBorderWidth: 6,
        }]
      },
      plugins: [eventMarks, crosshairPlugin('pulseCrosshair', faint)],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: {mode: 'index', intersect: false},
        layout: {padding: {top: 6}},
        plugins: {
          legend: {display: false},
          tooltip: {
            backgroundColor: tipBg,
            titleColor: mutedC,
            bodyColor: inkC,
            borderColor: gridC,
            borderWidth: 1,
            titleFont: {family: CHART_MONO, size: 10, weight: '600'},
            bodyFont: {family: CHART_MONO, size: 12.5, weight: '700'},
            padding: 10,
            displayColors: false,
            cornerRadius: 8,
            caretSize: 5,
            callbacks: {
              title: (items) => items[0].label.toUpperCase(),
              label: (c) => 'INDEX  ' + c.parsed.y.toFixed(3)
            }
          }
        },
        scales: {
          x: {
            grid: {display: false},
            border: {display: false},
            ticks: {color: faint, font: {family: CHART_MONO, size: 9.5, weight: '500'}, maxTicksLimit: 5, maxRotation: 0, padding: 8}
          },
          y: {
            position: 'right',
            min: lo, max: hi,
            border: {display: false},
            grid: {color: chartAlpha(gridC, 0.55), drawTicks: false},
            ticks: {color: faint, font: {family: CHART_MONO, size: 9.5, weight: '500'}, maxTicksLimit: 4, padding: 10, callback: (v) => Number(v).toFixed(2)}
          }
        }
      }
    });
    return () => { if (chartRef.current){ chartRef.current.destroy(); chartRef.current = null; } };
  }, [Array.isArray(data) ? data.join(',') : '', color, dir, height, eventsSig, ChartConstructor]);

  if (!Array.isArray(data) || data.length < 2) return null;
  const label = 'Signal index over ' + data.length + ' days, latest value ' + data[data.length - 1].toFixed(3) + '.';
  return <div className="mchart" style={{height: height + 'px'}}><canvas ref={canvasRef} role="img" aria-label={label} /></div>;
}

/* ==== compare chart: overlay 2-3 signals, shared crosshair tooltip ==== */
export function CompareChart({series, height = 230}){
  const canvasRef = useRef(null);
  const chartRef = useRef(null);
  const dir = useDir();

  const usable = (series || []).filter((s) => Array.isArray(s.data) && s.data.length > 1);
  const sig = usable.map((s) => s.label + s.color + s.data.join(',')).join('|');
  const ChartConstructor = useChartConstructor(usable.length > 0);

  useEffect(() => {
    if (!ChartConstructor || !canvasRef.current || !usable.length) return;
    const faint = resolveColor('var(--faint)');
    const gridC = resolveColor('var(--line)');
    const tipBg = resolveColor('var(--surface-2)');
    const inkC = resolveColor('var(--ink)');
    const mutedC = resolveColor('var(--muted)');
    const n = usable[0].data.length;
    const labels = dateLabels(n);
    const all = usable.flatMap((s) => s.data);
    const min = Math.min(...all), max = Math.max(...all);
    const lo = Math.max(0, min - (max - min) * 0.15), hi = Math.min(1, max + (max - min) * 0.15);

    const datasets = usable.map((s) => {
      const c = resolveColor(s.color);
      return {
        label: s.label,
        data: s.data,
        borderColor: c,
        borderWidth: 2.2,
        tension: 0.4,
        cubicInterpolationMode: 'monotone',
        fill: false,
        pointRadius: (ctx) => ctx.dataIndex === n - 1 ? 3 : 0,
        pointBackgroundColor: c,
        pointBorderColor: c,
        pointHoverRadius: 4,
        pointStyle: 'circle',
      };
    });

    if (chartRef.current) chartRef.current.destroy();
    chartRef.current = new ChartConstructor(canvasRef.current.getContext('2d'), {
      type: 'line',
      data: {labels, datasets},
      plugins: [crosshairPlugin('pulseCrosshair2', faint)],
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode: 'index', intersect: false},
        layout: {padding: {top: 6}},
        plugins: {
          legend: {display: false},
          tooltip: {
            backgroundColor: tipBg, titleColor: mutedC, bodyColor: inkC, borderColor: gridC, borderWidth: 1,
            titleFont: {family: CHART_MONO, size: 10, weight: '600'},
            bodyFont: {family: CHART_MONO, size: 11.5, weight: '600'},
            padding: 10, displayColors: true, boxWidth: 7, boxHeight: 7, boxPadding: 4, usePointStyle: true, cornerRadius: 8, caretSize: 5,
            callbacks: {
              title: (items) => items[0].label.toUpperCase(),
              label: (c) => ' ' + c.dataset.label.toUpperCase() + '  ' + c.parsed.y.toFixed(3)
            }
          }
        },
        scales: {
          x: {grid: {display: false}, border: {display: false}, ticks: {color: faint, font: {family: CHART_MONO, size: 9.5, weight: '500'}, maxTicksLimit: 5, maxRotation: 0, padding: 8}},
          y: {position: 'right', min: lo, max: hi, border: {display: false}, grid: {color: chartAlpha(gridC, 0.55), drawTicks: false}, ticks: {color: faint, font: {family: CHART_MONO, size: 9.5, weight: '500'}, maxTicksLimit: 4, padding: 10, callback: (v) => Number(v).toFixed(2)}}
        }
      }
    });
    return () => { if (chartRef.current){ chartRef.current.destroy(); chartRef.current = null; } };
  }, [sig, dir, height, ChartConstructor]);

  if (!usable.length) return null;
  const label = 'Comparison over ' + usable[0].data.length + ' days of ' + usable.map((s) => s.label + ' (latest ' + s.data[s.data.length - 1].toFixed(3) + ')').join(', ') + '.';
  return <div className="mchart" style={{height: height + 'px'}}><canvas ref={canvasRef} role="img" aria-label={label} /></div>;
}

/* ==== radar: tone × velocity quadrant scatter over the whole desk ==== */
export function RadarChart({topics, onOpen, height = 320}){
  const canvasRef = useRef(null);
  const chartRef = useRef(null);
  const dir = useDir();

  const usable = (topics || []).filter((t) => typeof t.delta === 'number' && typeof t.sentiment === 'number');
  const sig = usable.map((t) => t.id + t.delta + t.sentiment).join('|');
  const ChartConstructor = useChartConstructor(usable.length > 0);

  useEffect(() => {
    if (!ChartConstructor || !canvasRef.current || !usable.length) return;
    const faint = resolveColor('var(--faint)');
    const gridC = resolveColor('var(--line)');
    const tipBg = resolveColor('var(--surface-2)');
    const inkC = resolveColor('var(--ink)');
    const mutedC = resolveColor('var(--muted)');
    const upC = resolveColor('var(--up)');
    const downC = resolveColor('var(--down)');

    const pts = usable.map((t) => ({x: t.delta, y: t.sentiment, topic: t}));
    const colors = usable.map((t) => resolveColor((MOM_META[t.momentum] || MOM_META.steady).color));
    const maxX = Math.max(0.035, ...usable.map((t) => Math.abs(t.delta) * 1.25));

    const quadrants = {
      id: 'pulseQuadrants',
      beforeDatasetsDraw(ch){
        const {left, right, top, bottom} = ch.chartArea;
        const x0 = ch.scales.x.getPixelForValue(0);
        const y0 = ch.scales.y.getPixelForValue(0);
        const c = ch.ctx;
        c.save();
        /* dashed zero-axis cross */
        c.setLineDash([3, 4]); c.strokeStyle = chartAlpha(faint, 0.6); c.lineWidth = 1;
        c.beginPath(); c.moveTo(x0, top); c.lineTo(x0, bottom); c.stroke();
        c.beginPath(); c.moveTo(left, y0); c.lineTo(right, y0); c.stroke();
        c.setLineDash([]);
        /* quadrant labels */
        c.font = '700 9px JetBrains Mono';
        c.fillStyle = chartAlpha(upC, 0.9);
        c.textAlign = 'right'; c.fillText('ACTIVATE', right - 8, top + 14);
        c.fillStyle = chartAlpha(downC, 0.9);
        c.fillText('PR RADAR', right - 8, bottom - 8);
        c.fillStyle = chartAlpha(mutedC, 0.8);
        c.textAlign = 'left'; c.fillText('HOLD', left + 8, top + 14);
        c.fillText('LOW PRIORITY', left + 8, bottom - 8);
        c.restore();
      }
    };

    if (chartRef.current) chartRef.current.destroy();
    chartRef.current = new ChartConstructor(canvasRef.current.getContext('2d'), {
      type: 'scatter',
      data: {datasets: [{data: pts, pointRadius: 7, pointHoverRadius: 9, pointBackgroundColor: colors.map((c) => chartAlpha(c, 0.85)), pointBorderColor: colors, pointBorderWidth: 1.5}]},
      plugins: [quadrants],
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        onClick: (e, els) => { if (els.length && onOpen) onOpen(pts[els[0].index].topic); },
        onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
        plugins: {
          legend: {display: false},
          tooltip: {
            backgroundColor: tipBg, titleColor: inkC, bodyColor: mutedC, borderColor: gridC, borderWidth: 1,
            titleFont: {family: CHART_MONO, size: 11.5, weight: '700'},
            bodyFont: {family: CHART_MONO, size: 10.5, weight: '500'},
            padding: 10, displayColors: false, cornerRadius: 8,
            callbacks: {
              title: (items) => pts[items[0].dataIndex].topic.topic.toUpperCase(),
              label: (c) => ['Δ ' + (c.parsed.x >= 0 ? '+' : '') + c.parsed.x.toFixed(3) + '  ·  tone ' + c.parsed.y.toFixed(2), pts[c.dataIndex].topic.regionName + ' · click to open brief']
            }
          }
        },
        scales: {
          x: {min: -maxX, max: maxX, border: {display: false}, grid: {display: false},
            title: {display: true, text: 'VELOCITY  Δ / DAY', color: faint, font: {family: CHART_MONO, size: 8.5, weight: '600'}},
            ticks: {color: faint, font: {family: CHART_MONO, size: 9}, maxTicksLimit: 5, callback: (v) => (v >= 0 ? '+' : '') + Number(v).toFixed(2)}},
          y: {min: -0.9, max: 0.9, border: {display: false}, grid: {display: false},
            title: {display: true, text: 'TONE', color: faint, font: {family: CHART_MONO, size: 8.5, weight: '600'}},
            ticks: {color: faint, font: {family: CHART_MONO, size: 9}, maxTicksLimit: 5, callback: (v) => Number(v).toFixed(1)}}
        }
      }
    });
    return () => { if (chartRef.current){ chartRef.current.destroy(); chartRef.current = null; } };
  }, [sig, dir, height, ChartConstructor]);

  if (!usable.length) return null;
  const label = 'Scatter of ' + usable.length + ' topics plotting daily velocity against tone, to flag which signals are worth activating.';
  return <div className="mchart" style={{height: height + 'px'}}><canvas ref={canvasRef} role="img" aria-label={label} /></div>;
}

/* ==== count up ==== */
export function CountUp({to, dur = 1100, dp = 0, comma = false, className}){
  const [v, setV] = useState(0);
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const t0 = performance.now();
    const tick = (t) => {
      const p = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - p, 3);
      setV(to * e);
      if (p < 1) requestAnimationFrame(tick);
      else setV(to);
    };
    requestAnimationFrame(tick);
    const f = setTimeout(() => setV(to), dur + 400);
    return () => clearTimeout(f);
  }, [to]);
  return <span className={className}>{dp ? v.toFixed(dp) : (comma ? Math.round(v).toLocaleString() : Math.round(v))}</span>;
}

export function Sentiment({s}){
  if (typeof s !== 'number') return null;
  const lab = sentimentLabel(s);
  const cls = s > 0.15 ? 'pos' : s < -0.15 ? 'neg' : 'neu';
  return <span className={'sent ' + cls}><span className="swatch" /><span className="lab">{lab}</span></span>;
}

export function SignalLoader(){
  return <div className="signal-mark" role="status" aria-label="Loading">42</div>;
}

/* ==== V3 loader vocabulary (design_handoff_pulse_v3, PULSE V3 Index.dc.html
   "loading lab"). Same size conventions as SignalLoader: pure CSS animation,
   guarded by the global [data-motion="off"] rule in app.css and
   prefers-reduced-motion. Each loader takes isLoading (default true).
   While loading it announces role="status" and animates; when idle (a fetch
   settled empty) it carries no loading semantics and freezes on the same
   static frame the motion-off rules in gate.css use, via inline styles.
   Homes (round 2): SignalRings on Board, LogDrum on Listen, Orbit on
   Network, ScanSweep on Discover, via EmptyState below. ==== */
const loaderAria = (isLoading) => (isLoading ? {role: 'status', 'aria-label': 'Loading'} : {});

export function SignalRingsLoader({isLoading = true} = {}){
  const ring = isLoading ? undefined : {animation: 'none', transform: 'scale(0.75)', opacity: 0.4};
  const core = isLoading ? undefined : {animation: 'none'};
  return (
    <div className="ld-rings" {...loaderAria(isLoading)}>
      <span className="ld-ring" style={ring} /><span className="ld-ring" style={ring} />
      <span className="ld-core" style={core} />
    </div>
  );
}

export function LogDrumLoader({isLoading = true} = {}){
  const bar = isLoading ? undefined : {animation: 'none', transform: 'scaleY(0.6)'};
  return (
    <div className="ld-drum" {...loaderAria(isLoading)}>
      <span className="ld-bar" style={bar} /><span className="ld-bar" style={bar} /><span className="ld-bar" style={bar} /><span className="ld-bar" style={bar} />
    </div>
  );
}

export function OrbitLoader({isLoading = true} = {}){
  return (
    <div className="ld-orbit" {...loaderAria(isLoading)} style={isLoading ? undefined : {animation: 'none'}}>
      <span className="ld-sat" /><span className="ld-sat" /><span className="ld-sat" />
    </div>
  );
}

export function ScanSweepLoader({isLoading = true} = {}){
  const band = isLoading ? undefined : {animation: 'none', transform: 'translateX(60%)', opacity: 0.5};
  return (
    <div className="ld-sweep" {...loaderAria(isLoading)}>
      <span className="ld-band" style={band} />
    </div>
  );
}

/* ==== EmptyState (round 2, notes.md "loader homes"). One shared primitive:
   a branded loader with a job, a title, an honest body, a mono status row of
   real unit·window·market facts, and at least one forward action. Loaders
   animate on the motion setting and freeze static (never hide) under
   [data-motion="off"] / reduced motion via the rules in gate.css.
   isLoading (default false): most EmptyStates render after a fetch settled
   empty; pass true only while a fetch is genuinely in flight.
   Quiet register, 23 Sept 2026: the block is a plain, left-aligned passage
   with no fill, as rule 20 asks: the title says what happened, the body and
   the actions say what to do next. A settled state draws no loader, because
   a frozen drum above the words still reads as a wait that is not
   happening; the loader, with its role="status", shows only while
   isLoading. The es-plain rules live in app.css.
   status[]: strings, falsy entries dropped (real facts or omit the fact).
   actions[]: {label, onClick?|href?, primary?}. field: mono label rendered in
   the sweep scan field (Discover). ==== */
/* One wait state. Ring, orbit and sweep were three ways of performing the same
   fact, and a system that spends its state budget on waiting rather than
   refusing has its priorities inverted for a product that refuses most
   mornings. Retired names resolve to the drum rather than breaking a caller. */
const ES_LOADERS = {
  drum: LogDrumLoader,
  ring: LogDrumLoader,
  orbit: LogDrumLoader,
  sweep: LogDrumLoader,
};

export function EmptyState({loader = 'drum', field, title, body, status = [], actions = [], isLoading = false}){
  const hasLoader = loader !== false && loader !== null && loader !== '';
  const Loader = hasLoader ? (ES_LOADERS[loader] || LogDrumLoader) : null;
  const showsLoader = hasLoader && isLoading;
  const facts = (status || []).filter(Boolean);
  const acts = (actions || []).filter(Boolean);
  return (
    <div className={'es-block es-plain' + (hasLoader ? ' es-' + loader : '')}>
      {showsLoader && (loader === 'sweep' ? (
        <div className="es-field">
          <span className="es-field-label">
            <span className="es-field-dot" style={isLoading ? undefined : {animation: 'none', opacity: 0.6}} />{field || 'Scanning'}
          </span>
          <ScanSweepLoader isLoading={isLoading} />
        </div>
      ) : (
        <div className="es-loader"><Loader isLoading={isLoading} /></div>
      ))}
      <div className="es-copy">
        {title && <h2 className="es-title">{title}</h2>}
        {body && <p className="es-body">{body}</p>}
        {!!facts.length && (
          <div className="es-status">
            {facts.map((f, i) => (
              <span key={i} className="es-fact">
                {i > 0 && <span className="es-sep">&middot;</span>}
                <span>{f}</span>
              </span>
            ))}
          </div>
        )}
        {!!acts.length && (
          <div className="es-actions">
            {acts.map((a, i) => (
              a.href ? (
                <a key={i} className={a.primary ? 'es-act' : 'es-act-ghost'} href={a.href}>{a.label}</a>
              ) : (
                <button key={i} type="button" className={a.primary ? 'es-act' : 'es-act-ghost'} onClick={a.onClick}>{a.label}</button>
              )
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
