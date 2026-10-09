/* The market an Ask is sent for. The Market select starts on the page's
   region, so a question that names another market ("How are people in
   Nigeria talking about food prices?") would otherwise be read for the
   region instead. The place words are the agent's own (core/agent/ask.py
   _MARKET_PATTERNS and _SA); a test reads that file so the lists stay equal. */
const MARKET_WORDS = {
  ZA: /\b(?:south africa|mzansi|joburg|johannesburg|cape town|durban|heritage day)/i,
  NG: /\b(?:nigeria|naija|lagos|abuja)/i,
  KE: /\b(?:kenya|nairobi|mombasa|kisumu|nakuru|east africa)/i,
};
const SA_CAPITALS = /\bSA\b/;

/* Every market the question names, in the order first named. */
export function namedMarkets(question){
  const text = String(question || '');
  const first = {};
  for (const [market, pattern] of Object.entries(MARKET_WORDS)){
    const match = pattern.exec(text);
    if (match) first[market] = match.index;
  }
  const sa = SA_CAPITALS.exec(text);
  if (sa) first.ZA = Math.min(first.ZA === undefined ? sa.index : first.ZA, sa.index);
  return Object.keys(first).sort((a, b) => first[a] - first[b]);
}

/* A market the reader picked is sent as it is. Otherwise a question that
   names one market is read for that market, one that names several is left
   to the question (''), and one that names none keeps the select. */
export function marketToSend({question, selected, picked}){
  if (picked) return selected || '';
  const named = namedMarkets(question);
  if (named.length === 1) return named[0];
  if (named.length > 1) return '';
  return selected || '';
}
