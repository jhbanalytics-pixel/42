/* The cost confirm's consent for one live ask. It is held in memory only, never
   in the address or in storage, so no link can carry it. The confirm grants it
   for one question, market, item and date; the Ask page spends it once, within
   thirty seconds, and an address without a matching token opens as a draft. */
const LIFETIME_MS = 30000;
const keyOf = (query) => JSON.stringify([
  String((query && query.q) || ''),
  String((query && query.market) || ''),
  String((query && query.item) || ''),
  String((query && query.date) || ''),
]);

let granted = null;

export function grantAsk(query){
  granted = {key: keyOf(query), at: Date.now()};
}

export function consumeAsk(query){
  const token = granted;
  granted = null;
  return Boolean(token) && Date.now() - token.at <= LIFETIME_MS && token.key === keyOf(query);
}
