/* Static boundary checks: Ask and Research backends stay separate (v4.1 plan). */
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const read = (rel) => readFileSync(`${root}/${rel}`, 'utf8');

let failed = 0;
const fail = (msg) => { console.error('FAIL:', msg); failed++; };

const chatPy = read('src/api/chat.py');
for (const token of ['research', 'synth', 'persona_registry']){
  const re = new RegExp(`from \\.${token}|import ${token}`);
  if (re.test(chatPy)) fail(`chat.py must not import ${token}`);
}

const chatJsx = read('frontend/src/chat.jsx');
if (/window\.location\.hash\s*=/.test(chatJsx)){
  fail('chat.jsx must not write window.location.hash directly');
}

const briefJsx = read('frontend/src/ResearchDocPanel.jsx');
const routeJs = read('frontend/src/workbenchRoute.js');
if (/window\.location\.hash\s*=/.test(briefJsx)){
  fail('ResearchDocPanel.jsx must not write window.location.hash directly');
}
if (/navigateWorkbench\s*\(/.test(briefJsx)){
  fail('ResearchDocPanel.jsx must route through shell callbacks, not navigateWorkbench');
}
if (/canAutoStartResearch|autoStarted/.test(briefJsx + routeJs)){
  fail('route hydration must not retain a research auto-start surface');
}

const workbench = read('frontend/src/ConsoleWorkbench.jsx');
if (!workbench.includes('parseWorkbenchRoute') || !workbench.includes('navigateWorkbench')){
  fail('ConsoleWorkbench.jsx must own workbench navigation');
}

const share = read('frontend/src/researchLib.jsx') + read('frontend/src/workbenchRoute.js');
if (!share.includes('buildWorkbenchHash') || !share.includes('#/console')){
  fail('researchLib.jsx must emit console workbench share links');
}

/* The Client Read must be reachable. A dossier no route mounts is not an
   artifact a strategist can open, link to, or put in front of anyone. */
if (!workbench.includes('IntelligenceDossier')){
  fail('ConsoleWorkbench.jsx must mount IntelligenceDossier');
}

/* The Client Read is the artifact preview inside the investigation, which is
   the only hash the contract gives it. It is not a work state of its own, and
   inventing one would scaffold around the route the contract fixes. */
if (/work === 'read'|work: 'read'/.test(workbench + routeJs)){
  fail('the Console must not invent a read work state; the contract hash is work=brief with an artifact');
}
if (!workbench.includes('IntelligenceDossier')){
  fail('ConsoleWorkbench.jsx must mount IntelligenceDossier');
}
/* Keyed by both identities. An artifact without its investigation is
   workspace_request_invalid, so the preview may never render on one alone. */
/* Opening a read reads. Rendering the preview may not start research or a
   model call. */
if (/artifactId[\s\S]{0,300}?(startResearch|generateArtifact)/.test(workbench)){
  fail('the client read preview must not start work');
}
if (!/isBrief && routeState\.investigationId && routeState\.artifactId/.test(workbench)){
  fail('the client read preview must require both investigation and artifact');
}

/* The recents rail says loading until the read comes back. Both the success
   and the failure path must mark it complete, or a rail that never resolves
   sits in loading forever, which is a different lie from the empty one this
   replaced. */
const loadedMarks = (workbench.match(/setRecentLoaded\(true\)/g) || []).length;
if (loadedMarks < 2){
  fail(`the recents read must mark itself complete on both success and failure (found ${loadedMarks})`);
}

/* Focus follows the investigation. Opening a different investigation changes
   everything on the stage, so a reader who does not move with it is left
   reading the previous one's heading. */
const focusDeps = (workbench.match(/\}, \[work, routeState\.[^\]]*\]\);/g) || []).join('');
if (!/investigationId/.test(focusDeps)){
  fail('the Console stage focus effect must depend on the investigation as well as work and artifact');
}

/* The reduced-motion bridge. app.css carries a full [data-motion="off"]
   suppression and a comment saying App.jsx sets it, but nothing in the source
   or the built bundle ever did. A rule keyed on an attribute no one sets is
   not a reduced-motion implementation, and several matrix cells were recorded
   proven on the strength of it. */
const shell = read('frontend/src/App.jsx');
if (!/data-motion/.test(shell)){
  fail('App.jsx must set data-motion, which app.css keys its motion suppression on');
}
if (!/prefers-reduced-motion/.test(shell)){
  fail('the motion bridge must read the reduced-motion preference rather than guess');
}
/* Defined is not called. A text check proves the bridge exists in the file,
   which is exactly what was already true of the comment in app.css claiming
   this bridge was here. The call site is what makes it run. */
const appBody = shell.slice(shell.indexOf('export default function App()'));
if (!/^\s*useReducedMotionBridge\(\);/m.test(appBody)){
  fail('App must call the reduced-motion bridge, not merely define it');
}

/* The passcode gate takes over before any route renders. It does today, by an
   early return, and nothing asserted the ordering. A refactor that moved the
   gate below the routes would leave a workspace's last data on screen behind
   an authentication failure, which is the one moment stale content is least
   excusable. */
const gateAt = shell.indexOf('if (needPass) return');
const firstRouteAt = shell.indexOf("route === 'pulse'");
if (gateAt < 0){
  fail('App must gate on the passcode before rendering routes');
} else if (firstRouteAt >= 0 && gateAt > firstRouteAt){
  fail('the passcode gate must return before any route renders, or stale content survives an auth failure');
}

/* The entrance is mounted. The contract makes #/console the dossier landing
   with recent investigations, and work=brief the place a new one is framed.
   Both surfaces existed and neither was reachable. */
/* Rendered, not merely imported. An import satisfies a name check while the
   surface is mounted nowhere, which is exactly how a reachable route stays
   unreachable. */
if (!/<InvestigationIndex\b/.test(workbench)){
  fail('the Console landing must render the investigation index, not merely import it');
}
if (!/<InvestigationFraming\b/.test(workbench)){
  fail('the Console must render the framing surface, not merely import it');
}
/* And each must be told whether its read has finished, or an unfinished read
   shows as an empty estate. */
if (!/<InvestigationIndex[^>]*loading=\{![A-Za-z]/.test(workbench)){
  fail('the investigation index must be told whether its read has returned');
}
if (!/<InvestigationFraming[^>]*loading=\{![A-Za-z]/.test(workbench)){
  fail('the framing surface must be told whether its read has returned');
}
/* Reads only. Opening the landing lists what exists; it must not create,
   generate or spend to populate itself. */
if (/investigations\/list[\s\S]{0,200}?(generate|research\/generate|prepare)/.test(workbench)){
  fail('the landing read must not start work');
}

if (failed){
  console.error(`FAIL: ${failed} console boundary check(s)`);
  process.exit(1);
}
console.log('OK: console boundary checks passed');
