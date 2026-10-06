/* Pure batch fan-out helpers (no React). One generate job per approved behaviour. */

export function focusNoteForBehaviour(b){
  const base = String(b.behaviour || b.signal_topic || '').trim();
  const note = String(b.note || '').trim();
  if (!base) return note;
  return note ? base + ' [note: ' + note + ']' : base;
}

export function focusPayloadForBehaviour(b){
  const qg = String(b.query_group || '').trim();
  return {
    queryGroups: qg ? [qg] : [],
    note: focusNoteForBehaviour(b),
    markets: b.market ? [String(b.market).toLowerCase()] : [],
    examples: Array.isArray(b.examples) ? b.examples : [],
  };
}

export function behaviourBriefLabel(b, index){
  const mk = String((b && b.market) || '').toUpperCase();
  const topic = (b && (b.signal_topic || b.behaviour)) || ('Brief ' + (index + 1));
  return mk ? mk + ' · ' + topic : topic;
}

/** Sequential fan-out: one job per behaviour row. generateOne is injected for tests. */
export async function runGenerateBatch(personaId, fallbackMarkets, productFrame, behaviours, generateOne, onBatchProgress){
  const list = (behaviours || []).filter(Boolean);
  if (!list.length) throw new Error('No behaviours to generate.');
  const results = [];
  let parentId = null;
  for (let i = 0; i < list.length; i++){
    const b = list[i];
    const focus = focusPayloadForBehaviour(b);
    const mks = focus.markets.length ? focus.markets : fallbackMarkets;
    if (onBatchProgress) {
      onBatchProgress({index: i, total: list.length, behaviour: b, status: 'gathering_seeds', phase: 'running'});
    }
    const res = await generateOne({
      personaId,
      markets: mks,
      productFrame,
      focus,
      parentArtifactId: parentId,
      onProgress: (status) => {
        if (onBatchProgress) {
          onBatchProgress({index: i, total: list.length, behaviour: b, status, phase: 'running'});
        }
      },
    });
    if (res && res.artifact_id && !parentId) parentId = res.artifact_id;
    results.push({behaviour: b, result: res});
    if (onBatchProgress) {
      onBatchProgress({
        index: i,
        total: list.length,
        behaviour: b,
        status: (res && res.status) || 'completed',
        phase: 'done',
        result: res,
      });
    }
  }
  return {results, parentArtifactId: parentId};
}
