export const TOPIC_PINS_KEY = 'pulse-watch';
const MARKETS = new Set(['za', 'ng', 'ke']);

export function sourceTopicPin(topic){
  const market = typeof topic?.region === 'string' ? topic.region.toLowerCase() : '';
  return typeof topic?.id === 'string' && topic.id.trim() && MARKETS.has(market) ? {id: topic.id, market} : null;
}

export function topicPinKey(pin){
  return typeof pin === 'string' ? 'legacy-topic:' + pin : 'topic-pin:' + pin.market + ':' + pin.id;
}

export function readTopicPins(storage){
  try {
    const pins = JSON.parse((storage ?? globalThis.localStorage).getItem(TOPIC_PINS_KEY) || '[]');
    if (!Array.isArray(pins)) throw new Error('Invalid pin collection');
    const normalized = pins.map(pin => {
      if (typeof pin === 'string' && pin.trim()) return pin;
      if (!pin || typeof pin !== 'object' || Array.isArray(pin) || Object.keys(pin).length !== 2 || !Object.hasOwn(pin, 'id') || !Object.hasOwn(pin, 'market')) throw new Error('Unknown pin shape');
      const scoped = sourceTopicPin({id: pin.id, region: pin.market});
      if (!scoped) throw new Error('Invalid pin identity');
      return scoped;
    });
    return {status: 'ready', pins: normalized.filter((pin, index) => normalized.findIndex(other => topicPinKey(other) === topicPinKey(pin)) === index)};
  } catch {return {status: 'unavailable', pins: []};}
}

export function changeTopicPins(change, storage){
  const current = readTopicPins(storage);
  if (current.status !== 'ready') return current;
  try {
    const pins = change(current.pins);
    if (pins === current.pins) return current;
    (storage ?? globalThis.localStorage).setItem(TOPIC_PINS_KEY, JSON.stringify(pins));
    return {status: 'ready', pins};
  } catch {return {...current, status: 'unavailable'};}
}

export function clearTopicPrior(pin, storage){
  try {
    const target = storage ?? globalThis.localStorage;
    const history = JSON.parse(target.getItem('pulse-lastlook') || '{}');
    if (!history || typeof history !== 'object' || Array.isArray(history)) return false;
    const key = typeof pin === 'string' ? 'topic:' + pin : topicPinKey(pin);
    if (!Object.hasOwn(history, key)) return true;
    delete history[key];
    target.setItem('pulse-lastlook', JSON.stringify(history));
    return true;
  } catch {return false;}
}

export function validTopicRows(rows){
  return Array.isArray(rows) && rows.every(row => sourceTopicPin(row));
}

export function resolveTopicPin(pin, rows){
  const legacy = typeof pin === 'string';
  const id = legacy ? pin : pin.id;
  const base = {pin, id, key: topicPinKey(pin), market: legacy ? null : pin.market, found: false};
  const readable = validTopicRows(rows);
  if (legacy){
    const markets = readable ? [...new Set(rows.filter(row => row.id === id).map(row => sourceTopicPin(row).market))] : [];
    return {...base, state: 'legacy', choices: ['za', 'ng', 'ke'].filter(market => markets.includes(market))};
  }
  if (!readable) return {...base, state: 'unavailable'};
  const matches = rows.filter(row => row.id === id && sourceTopicPin(row).market === pin.market);
  if (matches.length !== 1) return {...base, state: matches.length ? 'ambiguous' : 'missing'};
  const row = matches[0];
  const series = Array.isArray(row.series) && row.series.every(value => typeof value === 'number' && Number.isFinite(value) && value >= 0) ? row.series : [];
  return {...base, state: 'ready', found: true, name: typeof row.topic === 'string' && row.topic.trim() ? row.topic : id, momentum: row.momentum, series, flag: pin.market.toUpperCase()};
}
