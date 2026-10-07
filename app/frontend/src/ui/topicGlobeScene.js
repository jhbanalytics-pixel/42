/* The topic globe's drawing (three.js), loaded only when the Communities
   page has a globe to draw, so the 3D library never sits in the first
   bundle (the chart.js pattern in parts.jsx).

   What it draws comes from ui/topicGlobeModel.js: communities as points in
   their market's mark colour sized by creators, the topics they share as
   small ink points, and a lifted arc from each community to each of its
   topics. Red marks the chosen community only (nightdesk.css, colour means
   something or it is not used).

   Phones and speed: the pixel ratio is capped at 2, frames are drawn only
   while something moves and the globe is on screen, a horizontal drag turns
   it while a vertical one still scrolls the page (touch-action: pan-y), and
   prefers-reduced-motion drops the idle spin and the turn to a pick. */
import {
  BufferGeometry, Color, Float32BufferAttribute, Group, LineBasicMaterial, LineSegments, Mesh, MeshBasicMaterial,
  PerspectiveCamera, Quaternion, SRGBColorSpace, Scene, SphereGeometry, Vector3, WebGLRenderer,
} from 'three';

const FOV = 32;
const FIT = 0.78;
const LIFT = 1.012;
const ARC_STEPS = 24;
const SPIN = 0.06; // radians a second while idle
const TURN_MS = 700;
const PICK_PX = 28;
const TAP_PX = 6;

const ease = (t) => 1 - Math.pow(1 - t, 3);

function cssColour(el, name, fallback){
  const v = getComputedStyle(el).getPropertyValue(name).trim();
  try { return new Color(v || fallback); } catch { return new Color(fallback); }
}

/* The colour the globe sits on: the first ancestor with a background. */
function pageColour(el){
  for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
    const bg = getComputedStyle(node).backgroundColor;
    const m = /rgba?\(([^)]+)\)/.exec(bg || '');
    if (!m) continue;
    const [r, g, b, a = '1'] = m[1].split(',').map((x) => x.trim());
    if (Number(a) > 0.5) return new Color().setRGB(Number(r) / 255, Number(g) / 255, Number(b) / 255, SRGBColorSpace);
  }
  return new Color('#F5EEE4');
}

/* Body, grid and arcs are mixed from the page's own background and ink, so
   the globe sits a step off the page in Daylight and Midnight alike. */
function palette(el, market){
  const mark = '--mk-' + String(market || '').toLowerCase();
  const page = pageColour(el);
  const ink = cssColour(el, '--ink', '#15120F');
  /* Mixed in sRGB, as CSS mixes, not in three's linear working space. */
  const a = page.getRGB({r: 0, g: 0, b: 0}, SRGBColorSpace);
  const b = ink.getRGB({r: 0, g: 0, b: 0}, SRGBColorSpace);
  const mix = (k) => new Color().setRGB(a.r + (b.r - a.r) * k, a.g + (b.g - a.g) * k, a.b + (b.b - a.b) * k, SRGBColorSpace);
  return {
    body: mix(0.06),
    grid: mix(0.16),
    edge: mix(0.3),
    arc: mix(0.32),
    topic: mix(0.62),
    shared: ink.clone(),
    community: cssColour(el, mark, '#FF9A3D'),
    accent: cssColour(el, '--accent', '#E41424'),
    dim: mix(0.4),
  };
}

/* Latitude and longitude rings every 30 degrees, as one set of segments. */
function gridGeometry(){
  const pts = [];
  const ring = (fn) => {
    for (let i = 0; i < 96; i++) {
      const a = (i / 96) * Math.PI * 2;
      const b = ((i + 1) / 96) * Math.PI * 2;
      pts.push(...fn(a), ...fn(b));
    }
  };
  for (let lat = -60; lat <= 60; lat += 30) {
    const y = Math.sin((lat * Math.PI) / 180);
    const r = Math.cos((lat * Math.PI) / 180);
    ring((a) => [r * Math.cos(a), y, r * Math.sin(a)]);
  }
  for (let lon = 0; lon < 180; lon += 30) {
    const l = (lon * Math.PI) / 180;
    ring((a) => [Math.cos(a) * Math.cos(l), Math.sin(a), Math.cos(a) * Math.sin(l)]);
  }
  const g = new BufferGeometry();
  g.setAttribute('position', new Float32BufferAttribute(pts, 3));
  return g;
}

/* A great-circle arc from a to b, lifted off the surface in the middle. */
function arcPoints(a, b){
  const va = new Vector3(...a);
  const vb = new Vector3(...b);
  const angle = va.angleTo(vb);
  const lift = 0.04 + 0.22 * angle;
  const out = [];
  for (let i = 0; i <= ARC_STEPS; i++) {
    const t = i / ARC_STEPS;
    const p = angle < 1e-6 ? va.clone() : va.clone().multiplyScalar(Math.sin((1 - t) * angle) / Math.sin(angle))
      .add(vb.clone().multiplyScalar(Math.sin(t * angle) / Math.sin(angle)));
    out.push(p.normalize().multiplyScalar(LIFT + lift * Math.sin(Math.PI * t)));
  }
  return out;
}

/* The turn that brings p to face the viewer (+z), keeping north up. */
function facing(p){
  const yaw = Math.atan2(p[0], p[2]);
  const pitch = Math.asin(Math.max(-1, Math.min(1, p[1])));
  const q = new Quaternion().setFromAxisAngle(new Vector3(1, 0, 0), Math.max(-0.9, Math.min(0.9, pitch)));
  return q.multiply(new Quaternion().setFromAxisAngle(new Vector3(0, 1, 0), -yaw));
}

export function createGlobeScene(host, model, {market, onPick, reducedMotion = false, labels = null} = {}){
  const renderer = new WebGLRenderer({antialias: true, alpha: true, powerPreference: 'low-power'});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  const canvas = renderer.domElement;
  canvas.className = 'cg42-canvas';
  canvas.setAttribute('aria-hidden', 'true');
  canvas.style.cursor = 'grab';
  host.appendChild(canvas);

  const scene = new Scene();
  const camera = new PerspectiveCamera(FOV, 1, 0.1, 50);
  const globe = new Group();
  scene.add(globe);

  let colours = palette(host, market);
  const disposables = [];
  const keep = (x) => { disposables.push(x); return x; };

  /* An opaque body hides the far side, as a globe does; points and arcs on
     it come round as the globe turns. A ring in the screen plane draws the
     globe's edge. */
  const bodyMat = keep(new MeshBasicMaterial({color: colours.body}));
  globe.add(new Mesh(keep(new SphereGeometry(0.998, 64, 40)), bodyMat));
  const gridMat = keep(new LineBasicMaterial({color: colours.grid}));
  globe.add(new LineSegments(keep(gridGeometry()), gridMat));
  const edgeMat = keep(new LineBasicMaterial({color: colours.edge}));
  const edgePts = [];
  for (let i = 0; i < 128; i++) {
    const a = (i / 128) * Math.PI * 2;
    const b = ((i + 1) / 128) * Math.PI * 2;
    edgePts.push(Math.cos(a), Math.sin(a), 0, Math.cos(b), Math.sin(b), 0);
  }
  const edgeGeo = keep(new BufferGeometry());
  edgeGeo.setAttribute('position', new Float32BufferAttribute(edgePts, 3));
  const edge = new LineSegments(edgeGeo, edgeMat);
  scene.add(edge);

  const unitSphere = keep(new SphereGeometry(1, 20, 14));
  const communityById = new Map();
  const nodes = model.communities.map((c) => {
    const mat = keep(new MeshBasicMaterial({color: colours.community}));
    const mesh = new Mesh(unitSphere, mat);
    mesh.position.set(...c.pos).multiplyScalar(1 + c.size * 0.8);
    mesh.scale.setScalar(c.size);
    globe.add(mesh);
    const node = {c, mesh, mat};
    communityById.set(c.id, node);
    return node;
  });
  const topicNodes = model.topics.map((t) => {
    const mat = keep(new MeshBasicMaterial({color: t.shared ? colours.shared : colours.topic}));
    const mesh = new Mesh(unitSphere, mat);
    const r = t.shared ? 0.022 : 0.015;
    mesh.position.set(...t.pos).multiplyScalar(1 + r);
    mesh.scale.setScalar(r);
    globe.add(mesh);
    return {t, mesh, mat};
  });

  /* Every arc in one geometry; its vertex colours change with the pick. */
  const arcVerts = [];
  const arcOwner = [];
  for (const link of model.links) {
    const from = communityById.get(link.community);
    const to = model.topics.find((t) => t.id === link.topic);
    if (!from || !to) continue;
    const pts = arcPoints(from.c.pos, to.pos);
    for (let i = 0; i < pts.length - 1; i++) {
      arcVerts.push(pts[i].x, pts[i].y, pts[i].z, pts[i + 1].x, pts[i + 1].y, pts[i + 1].z);
      arcOwner.push(link.community, link.community);
    }
  }
  const arcGeo = keep(new BufferGeometry());
  arcGeo.setAttribute('position', new Float32BufferAttribute(arcVerts, 3));
  arcGeo.setAttribute('color', new Float32BufferAttribute(new Array(arcOwner.length * 3).fill(0), 3));
  const arcMat = keep(new LineBasicMaterial({vertexColors: true}));
  globe.add(new LineSegments(arcGeo, arcMat));

  let selected = null;
  function paint(){
    const colour = arcGeo.getAttribute('color');
    arcOwner.forEach((owner, i) => {
      const c = owner === selected ? colours.accent : colours.arc;
      colour.setXYZ(i, c.r, c.g, c.b);
    });
    colour.needsUpdate = true;
    const mine = new Set(selected ? model.links.filter((l) => l.community === selected).map((l) => l.topic) : []);
    for (const n of nodes) {
      const on = n.c.id === selected;
      n.mat.color.copy(on ? colours.accent : colours.community);
      n.mesh.scale.setScalar(n.c.size * (on ? 1.2 : 1));
    }
    for (const n of topicNodes) {
      n.mat.color.copy(mine.has(n.t.id) ? colours.shared : (n.t.shared ? colours.topic : colours.dim));
    }
    bodyMat.color.copy(colours.body);
    gridMat.color.copy(colours.grid);
    edgeMat.color.copy(colours.edge);
  }

  /* Size: the globe fills FIT of the shorter side. */
  let width = 1;
  let height = 1;
  function resize(){
    width = Math.max(1, host.clientWidth);
    height = Math.max(1, host.clientHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    const v = (FOV * Math.PI) / 360;
    const h = Math.atan(Math.tan(v) * camera.aspect);
    camera.position.set(0, 0, 1 / (Math.sin(Math.min(v, h)) * FIT));
    camera.lookAt(0, 0, 0);
    camera.updateProjectionMatrix();
    /* Seen from distance d, a unit sphere's outline is a circle of radius
       sqrt(1 - 1/d^2) at depth 1/d towards the camera. */
    const d = camera.position.z;
    edge.position.set(0, 0, 1 / d);
    edge.scale.setScalar(Math.sqrt(1 - 1 / (d * d)));
    wake();
  }

  /* Labels: an HTML layer above the canvas, placed each frame, shown only
     for points facing the viewer. */
  const v = new Vector3();
  function screen(pos){
    v.set(...pos).multiplyScalar(LIFT).applyQuaternion(globe.quaternion);
    const front = v.z;
    v.project(camera);
    return {x: ((v.x + 1) / 2) * width, y: ((1 - v.y) / 2) * height, front};
  }
  /* Labels come in priority order (the pick first, then by size); one that
     would cover a label already placed waits until the globe turns. */
  function placeLabels(){
    if (!labels) return;
    const taken = [];
    /* Every size is read before any label moves, so a frame lays out once. */
    const els = [...labels.querySelectorAll('[data-pos]')].map((el) => ({el, w: el.offsetWidth || 0, h: el.offsetHeight || 0}));
    for (const {el, w, h} of els) {
      const pos = el.dataset.pos.split(',').map(Number);
      const {x, y, front} = screen(pos);
      const left = x > width * 0.6;
      el.style.transform = 'translate(' + x.toFixed(1) + 'px, ' + y.toFixed(1) + 'px)' + (left ? ' translateX(-100%) translateX(-24px)' : '');
      const box = {l: left ? x - 12 - w : x + 12, t: y - h * 0.75, w, h};
      const clear = !taken.some((o) => box.l < o.l + o.w && o.l < box.l + box.w && box.t < o.t + o.h && o.t < box.t + box.h);
      const show = front > 0.25 && clear;
      if (show) taken.push(box);
      el.style.opacity = show ? '1' : '0';
    }
  }

  /* Turning: drag, an idle spin, and a turn to a pick. */
  let spinning = !reducedMotion;
  let turn = null;
  let drag = null;
  let visible = true;
  let frame = 0;
  let last = 0;
  const yAxis = new Vector3(0, 1, 0);
  const xAxis = new Vector3(1, 0, 0);
  const tmp = new Quaternion();

  function draw(now){
    frame = 0;
    const dt = last ? Math.min(0.05, (now - last) / 1000) : 0;
    last = now;
    let moving = false;
    if (turn) {
      const t = Math.min(1, (now - turn.start) / TURN_MS);
      globe.quaternion.slerpQuaternions(turn.from, turn.to, ease(t));
      if (t >= 1) turn = null;
      moving = true;
    } else if (spinning && !drag) {
      tmp.setFromAxisAngle(yAxis, SPIN * dt);
      globe.quaternion.premultiply(tmp);
      moving = true;
    }
    renderer.render(scene, camera);
    placeLabels();
    if (moving || drag) wake(); else last = 0;
  }
  function wake(){
    if (!frame && visible && !document.hidden) frame = requestAnimationFrame(draw);
  }

  function pickAt(px, py){
    let best = null;
    let bestD = PICK_PX;
    for (const n of nodes) {
      const s = screen(n.c.pos);
      if (s.front <= 0) continue;
      const d = Math.hypot(s.x - px, s.y - py);
      if (d < bestD) { bestD = d; best = n.c.id; }
    }
    return best;
  }

  function onDown(e){
    spinning = false;
    turn = null;
    drag = {x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY, id: e.pointerId, touch: e.pointerType === 'touch'};
    try { canvas.setPointerCapture(e.pointerId); } catch { /* the pointer left already */ }
    canvas.style.cursor = 'grabbing';
    wake();
  }
  function onMove(e){
    if (!drag || e.pointerId !== drag.id) return;
    const dx = e.clientX - drag.x;
    const dy = drag.touch ? 0 : e.clientY - drag.y;
    drag.x = e.clientX;
    drag.y = e.clientY;
    const k = (Math.PI / Math.max(240, Math.min(width, height)));
    tmp.setFromAxisAngle(yAxis, dx * k);
    globe.quaternion.premultiply(tmp);
    if (dy) {
      tmp.setFromAxisAngle(xAxis, dy * k);
      globe.quaternion.premultiply(tmp);
    }
    wake();
  }
  function onUp(e){
    if (!drag || e.pointerId !== drag.id) return;
    const tap = Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < TAP_PX;
    drag = null;
    canvas.style.cursor = 'grab';
    if (tap && onPick) {
      const r = canvas.getBoundingClientRect();
      const id = pickAt(e.clientX - r.left, e.clientY - r.top);
      if (id) onPick(id);
    }
    wake();
  }
  canvas.addEventListener('pointerdown', onDown);
  canvas.addEventListener('pointermove', onMove);
  canvas.addEventListener('pointerup', onUp);
  /* A cancelled pointer (the page took the swipe as a scroll) never picks. */
  function onCancel(e){
    if (!drag || e.pointerId !== drag.id) return;
    drag = null;
    canvas.style.cursor = 'grab';
    wake();
  }
  canvas.addEventListener('pointercancel', onCancel);

  const sizes = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
  if (sizes) sizes.observe(host);
  const seen = typeof IntersectionObserver === 'function'
    ? new IntersectionObserver((entries) => { visible = entries.some((en) => en.isIntersecting); wake(); })
    : null;
  if (seen) seen.observe(host);
  const onVisibility = () => wake();
  document.addEventListener('visibilitychange', onVisibility);
  /* The theme switch (Midnight or Daylight) recolours the globe. */
  const themed = typeof MutationObserver === 'function' ? new MutationObserver(() => { colours = palette(host, market); paint(); wake(); }) : null;
  if (themed) {
    themed.observe(document.documentElement, {attributes: true, attributeFilter: ['data-dir']});
    const dir = host.closest('[data-dir]');
    if (dir && dir !== document.documentElement) themed.observe(dir, {attributes: true, attributeFilter: ['data-dir']});
  }

  const first = model.communities[0];
  if (first) globe.quaternion.copy(facing(first.pos)).premultiply(tmp.setFromAxisAngle(yAxis, 0.35));
  resize();
  paint();

  return {
    select(id, {turnTo = false} = {}){
      selected = id;
      paint();
      const node = communityById.get(id);
      if (turnTo && node) {
        spinning = false;
        const to = facing(node.c.pos);
        if (reducedMotion) globe.quaternion.copy(to);
        else turn = {from: globe.quaternion.clone(), to, start: performance.now()};
      }
      wake();
    },
    redraw: wake,
    dispose(){
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      visible = false;
      canvas.removeEventListener('pointerdown', onDown);
      canvas.removeEventListener('pointermove', onMove);
      canvas.removeEventListener('pointerup', onUp);
      canvas.removeEventListener('pointercancel', onCancel);
      document.removeEventListener('visibilitychange', onVisibility);
      if (sizes) sizes.disconnect();
      if (seen) seen.disconnect();
      if (themed) themed.disconnect();
      for (const d of disposables) d.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      canvas.remove();
    },
  };
}
