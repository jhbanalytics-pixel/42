/* Compare: aligned strips, never cards.

   The same field occupies the same column for every signal, so the reader
   compares like with like instead of re-reading a card per option. Values are
   the words the desk already publishes: a band, a readiness state, a receipt
   count. No engine score is admitted here either, and an unmeasured quality
   stays unmeasured rather than sliding to the bottom band. */

export const COMPARE_FIELDS = Object.freeze([
  {key: 'market', label: 'Market'},
  {key: 'readiness', label: 'Evidence readiness'},
  {key: 'why_now', label: 'Why now'},
  {key: 'precedent', label: 'Precedent availability'},
  {key: 'receipts', label: 'Direct receipts'},
  {key: 'velocity', label: 'Velocity'},
  {key: 'novelty', label: 'Novelty'},
  {key: 'breadth', label: 'Breadth'},
  {key: 'independence', label: 'Source independence'},
  {key: 'history', label: 'History'},
  {key: 'geo_confidence', label: 'Geographic confidence'},
]);

const QUALITY_KEYS = new Set(['velocity', 'novelty', 'breadth', 'independence', 'history', 'geo_confidence']);

function receiptLabel(count){
  const value = Number.isFinite(count) ? count : 0;
  return `${value} receipt${value === 1 ? '' : 's'}`;
}

function valueFor(signal, key){
  if (!signal || typeof signal !== 'object') return 'unavailable';
  if (key === 'market') return String(signal.market || '').toUpperCase() || 'unavailable';
  if (key === 'readiness') return signal.readiness || 'unchecked';
  if (key === 'why_now') return signal.whyNow || 'unavailable';
  if (key === 'precedent'){
    const history = signal.qualities && signal.qualities.history;
    return history && history !== 'unmeasured' ? 'available' : 'unavailable';
  }
  if (key === 'receipts') return receiptLabel(signal.receiptCount);
  if (QUALITY_KEYS.has(key)){
    const qualities = signal.qualities && typeof signal.qualities === 'object' ? signal.qualities : {};
    return qualities[key] || 'unmeasured';
  }
  return 'unavailable';
}

export function buildComparisonRows(signals){
  const rows = Array.isArray(signals) ? signals : [];
  /* One signal compares with nothing. Rendering it alone would read as a
     comparison that had been made and found no difference. */
  if (rows.length < 2) return [];
  return COMPARE_FIELDS.map(({key, label}) => {
    const values = rows.map((signal) => valueFor(signal, key));
    return {key, label, values, differs: new Set(values).size > 1};
  });
}

export function ComparisonStrip({signals}){
  const rows = Array.isArray(signals) ? signals : [];
  const comparison = buildComparisonRows(rows);

  if (!comparison.length){
    return (
      <section className="compare-strip compare-strip--empty" aria-label="Comparison">
        <p>
          Compare needs two signals. {rows.length === 1
            ? 'One signal is selected, so there is nothing to read it against yet.'
            : 'No signal is selected yet.'}
        </p>
      </section>
    );
  }

  return (
    <section className="compare-strip" aria-label="Comparison">
      <div className="compare-strip__head" role="row">
        <span className="compare-strip__label" role="columnheader">Field</span>
        {rows.map((signal) => (
          <span className="compare-strip__signal" role="columnheader" key={signal.signalId || signal.title}>
            {signal.title}
          </span>
        ))}
      </div>
      {comparison.map((row) => (
        <div className="compare-strip__row" role="row" key={row.key} data-differs={row.differs}>
          <span className="compare-strip__label" role="rowheader">{row.label}</span>
          {row.values.map((value, index) => (
            <span
              className="compare-strip__value"
              role="cell"
              key={(rows[index] && rows[index].signalId) || index}
              data-value={value}
            >{value}</span>
          ))}
        </div>
      ))}
    </section>
  );
}

export default ComparisonStrip;
