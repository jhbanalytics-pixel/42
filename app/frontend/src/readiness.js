// Readiness by construction. Components receive receipts, never a bare state.
// A handed `readiness` is accepted only downward (demote), never upward.
export function deriveReadiness(receipts, handed) {
  const rows = Array.isArray(receipts) ? receipts.filter(r => r && r.family) : [];
  const families = [...new Set(rows.map(r => r.family))];
  let derived;
  if (!families.length) derived = 'unchecked';
  else if (new Set(rows.map(r => r.direction || r.dir)).size > 1) derived = 'contradictory';
  else derived = families.length === 1 ? 'thin' : 'ready';
  const rank = {unchecked: 0, contradictory: 1, thin: 2, ready: 3};
  if (handed && rank[handed] < rank[derived]) return handed; // demotion only
  return derived;
}
export function proofLine(receipts) {
  const rows = Array.isArray(receipts) ? receipts.filter(r => r && r.family) : [];
  const families = [...new Set(rows.map(r => r.family))];
  if (!families.length) return 'No qualifying receipt.';
  if (families.length === 1) return '1 qualifying family: ' + families[0] + '. A second must agree.';
  return families.length + ' independent families agree: ' + families.join(', ') + '.';
}
