/* Suggestions come from the completed run only. The desk payload's
   dynamic_discovery member carries the run's admitted signals, and each chip
   on a search or ask surface is one admitted signal's name for the market in
   view. A run that admitted nothing yields an empty row, and an unavailable or
   malformed member yields the same: there is no fallback list anywhere. */

const READY = 'ready';

function signalOf(row){
  if (!row || typeof row !== 'object') return null;
  if (row.signal && typeof row.signal === 'object') return row.signal;
  return row;
}

export function runSuggestions(deskData, market, limit = 5){
  const dynamic = deskData && typeof deskData === 'object' ? deskData.dynamic_discovery : null;
  if (!dynamic || typeof dynamic !== 'object') return [];
  if (dynamic.status !== READY || !Array.isArray(dynamic.signals)) return [];
  const mk = String(market || 'all').trim().toLowerCase() || 'all';
  const cap = Number.isFinite(limit) && limit > 0 ? limit : 5;
  const out = [];
  const seen = new Set();
  for (const row of dynamic.signals){
    const sig = signalOf(row);
    if (!sig) continue;
    const name = typeof sig.signal_name === 'string' ? sig.signal_name.trim() : '';
    if (!name) continue;
    if (mk !== 'all' && String(sig.market || '').toLowerCase() !== mk) continue;
    const key = name.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(name);
    if (out.length >= cap) break;
  }
  return out;
}
