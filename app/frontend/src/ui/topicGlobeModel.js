/* The topic globe's layout: the Communities list as points on a unit sphere.

   A community is one point, spread over the sphere on a Fibonacci lattice so
   no two crowd each other, largest first. Its point grows by area with its
   creators (Cleveland and McGill: area, not radius, reads as amount). Each
   topic its members share is a smaller point beside it; a topic two or more
   communities share sits between them, with a line to each, so the globe
   shows which communities post about the same things.

   The layout reads only what the API sent (each community's id, label,
   creators Figure and its unflagged topic cards) and sorts before placing,
   so the same list gives the same globe on every load. */

const GOLDEN = Math.PI * (3 - Math.sqrt(5));
const MIN_CREATORS = 5;
const BASE_SIZE = 0.032;
const SHARED_STEP = 0.12;

const norm = ([x, y, z]) => {
  const l = Math.hypot(x, y, z) || 1;
  return [x / l, y / l, z / l];
};
const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const scale = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];

/* Two directions along the sphere's surface at p. */
function tangents(p){
  const up = Math.abs(p[1]) > 0.99 ? [1, 0, 0] : [0, 1, 0];
  const e1 = norm(cross(up, p));
  return [e1, norm(cross(p, e1))];
}

/* Point i of n on a Fibonacci lattice; the first sits towards the viewer. */
function lattice(i, n){
  const y = n === 1 ? 0 : 1 - (2 * (i + 0.5)) / n;
  const r = Math.sqrt(Math.max(0, 1 - y * y));
  const theta = i * GOLDEN;
  return norm([r * Math.sin(theta), y, r * Math.cos(theta)]);
}

const creatorsOf = (c) => {
  const v = c && c.creators && c.creators.value;
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
};

const EMPTY = () => ({communities: [], topics: [], links: []});

export function globeModel(list){
  if (!Array.isArray(list) || list.length === 0) return EMPTY();
  const sorted = list
    .filter((c) => c && c.community_id)
    .slice()
    .sort((a, b) => ((creatorsOf(b) ?? 0) - (creatorsOf(a) ?? 0)) || String(a.community_id).localeCompare(String(b.community_id)));
  const n = sorted.length;
  const spread = Math.min(0.38, 0.4 * (3.5 / Math.sqrt(n)));

  const communities = sorted.map((c, i) => {
    const creators = creatorsOf(c);
    return {
      id: c.community_id,
      label: String(c.label || ''),
      creators,
      size: BASE_SIZE * Math.sqrt(Math.max(creators ?? MIN_CREATORS, MIN_CREATORS) / MIN_CREATORS),
      pos: lattice(i, n),
    };
  });
  const at = new Map(communities.map((c) => [c.id, c]));

  /* Every topic once, with the communities that share it, in globe order. */
  const topics = new Map();
  sorted.forEach((c) => {
    (Array.isArray(c.topics) ? c.topics : []).forEach((card) => {
      const id = card && card.item_id;
      const title = card && typeof card.title === 'string' ? card.title.trim() : '';
      if (!id || !title) return;
      const kept = topics.get(id);
      if (kept) {
        if (!kept.communities.includes(c.community_id)) kept.communities.push(c.community_id);
      } else {
        topics.set(id, {id, title, market: card.market || null, communities: [c.community_id]});
      }
    });
  });

  /* Own topics ring their community; shared topics sit at the mean of
     theirs, nudged apart when several share the same communities. */
  const ownCount = new Map();
  const ownSeen = new Map();
  const sharedGroups = new Map();
  for (const t of topics.values()) {
    if (t.communities.length === 1) {
      ownCount.set(t.communities[0], (ownCount.get(t.communities[0]) || 0) + 1);
    } else {
      const key = t.communities.slice().sort().join('|');
      sharedGroups.set(key, (sharedGroups.get(key) || 0) + 1);
    }
  }
  const sharedSeen = new Map();
  const placed = [...topics.values()].map((t) => {
    if (t.communities.length === 1) {
      const centre = at.get(t.communities[0]).pos;
      const k = ownSeen.get(t.communities[0]) || 0;
      ownSeen.set(t.communities[0], k + 1);
      const phi = (k * 2 * Math.PI) / ownCount.get(t.communities[0]) + 0.6;
      const [e1, e2] = tangents(centre);
      const along = add(scale(e1, Math.cos(phi)), scale(e2, Math.sin(phi)));
      return {...t, shared: false, pos: norm(add(scale(centre, Math.cos(spread)), scale(along, Math.sin(spread))))};
    }
    const key = t.communities.slice().sort().join('|');
    const j = sharedSeen.get(key) || 0;
    sharedSeen.set(key, j + 1);
    let mean = t.communities.reduce((sum, id) => add(sum, at.get(id).pos), [0, 0, 0]);
    if (Math.hypot(...mean) < 1e-6) mean = add(at.get(t.communities[0]).pos, [0, 0.01, 0]);
    const p = norm(mean);
    const [e1] = tangents(p);
    const offset = (j - (sharedGroups.get(key) - 1) / 2) * SHARED_STEP;
    return {...t, shared: true, pos: norm(add(p, scale(e1, offset)))};
  });

  const links = placed.flatMap((t) => t.communities.map((community) => ({community, topic: t.id})));
  return {communities, topics: placed, links};
}
