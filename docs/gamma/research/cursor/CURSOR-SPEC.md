# 42 cursor spec: "Target Lock" reticle

Research source: https://www.awwwards.com/awwwards/collections/hovers-cursors-and-cute-interactions/ (466 entries; the page serves the first 32). I read the Awwwards entry pages (they only carry tags) and then opened the live sites in headless Chromium. On each site I moved the mouse, hovered targets, and dumped the cursor DOM, the computed styles and the source CSS. Behaviour described below comes from that real markup and CSS. Where a point is inferred from tags alone, it says so.

---

## 1. Collection entries (first page, 32)

| # | Entry | Site | Effect (tag or observed) |
|---|---|---|---|
| 1 | 3D Cursor Interaction | superevilgeniuscorp.com | Bracketed mono label cursor, exclusion blend, 3D scene (deep dive A) |
| 2 | Autoplay video on Hover | accordion.net.au/work | Video plays when you hover a project tile |
| 3 | Flower Page | botanicexpo.com | Product page, 3D flower reacts to the pointer |
| 4 | Titcket page | (Warhol Arts, BL/S) | Ticket-style hover interaction |
| 5 | Canvas grid | fiddle.digital | Cursor-reactive canvas grid over media (deep dive B) |
| 6 | Hover Buttons | restaurant-amici.com | Button fill and label roll on hover |
| 7 | P10 Cards | pesqueradiez.com | Cards tilt and lift under the pointer |
| 8 | 3D Object Interaction | (Viva la Labia, ROCANI) | 3D object rotates with the mouse |
| 9 | Texture hover reveal | duten.com | Hover reveals a material texture through a mask |
| 10 | Mouse Move Fluid Effect | (House of Dreamers) | WebGL fluid simulation trails the pointer |
| 11 | WebGL point cloud mouse effect | amaterasu.ai | Particles displaced by the cursor, plus a ring cursor (deep dive D) |
| 12 | Landing | alectear.com | Slider in "cursor mode" follows the pointer |
| 13 | Hover interaction | alectear.com/lettering | Lettering preview follows the hover |
| 14 | WebGL Footer | labs.chaingpt.org | Shader footer reacts to the mouse |
| 15 | Blog Rollover | (Mew, Griflan) | Image rollover on blog list rows |
| 16 | Interactive letters | radiance.family | Matter.js physics letters, "SHOW" pill cursor (deep dive I) |
| 17 | Hero Animation | wodniack.dev | Canvas hero plus a red contrast-mask wipe (deep dive H) |
| 18 | Keikku Device Interaction | keikku.health | Device model rotates with the pointer |
| 19 | Water Interaction WebGL | gentlerain.ai | Water ripple shader under the pointer |
| 20 | Shopify Summer '24 Hero | shopify.com/editions/summer2024 | 3D hero reacts to the mouse |
| 21 | Studio Gallery | madebyanalogue.co.uk | Draggable gallery with a drag cursor |
| 22 | Mouse Interactions | polecat.agency | Hover tags; I found no custom cursor DOM on /contacts |
| 23 | Hover Image Effect | gianlucagradogna.com | Image distorts and reveals on hover |
| 24 | Home Page Projects | stabondar.com | Pill cursor that morphs into a preview panel (deep dive E) |
| 25 | Mouse Interaction | photoyoshi.com | Three.js photo plane follows the mouse, zoom cursors |
| 26 | Site Intro Interaction | henriheymans.com | Intro loader that reacts to the mouse |
| 27 | Cursor Desktop | petragarmon.com | Label-pill cursor with states, idle hide, WebGL glitch (deep dive C) |
| 28 | Directors Text Hover | opos.buzzworthystudio.com | Hovering a name reveals the director's media |
| 29 | Cursor GSAP | ideology.it | GSAP cursor, text scramble, mouse image trail (deep dive G) |
| 30 | Fractal Hover Interaction | evasanchez.info | WebGL fractal distortion on hover (deep dive J) |
| 31 | Ripple Text Effect | evasanchez.info | Shader ripple on type |
| 32 | Strzok Radio | ryanstrzok.com | 54px SVG ring cursor with velocity skew (deep dive F) |

## 2. Deep dives (how each one is built)

**A. Super Evil Genius Corp (Jomor Design): bracketed label cursor.** A fixed full-viewport `.cursor-div` (z-index 500, `pointer-events:none`, `mix-blend-mode:exclusion`, uppercase, `letter-spacing:.1rem`) holds a 1x1px `.cursor` anchor. JS moves the anchor with `translate3d(vw,vh,0) scale3d() rotate()`. The anchor's children are state items (`is-scroll`, `is-play`, `is-contact` "work with us", `is-project`, `is-drag`), all `display:none` until their state is active. Each label is wrapped in two SVG half-brackets (path `M5.43 .5H.5V19.07H5.43` in a 6x20 viewBox, 0.5rem x 1.3rem); the right bracket is the same SVG mirrored with `rotateY(180deg)`. Labels sit 1rem right of and 1.5rem below the pointer. The native cursor stays visible. The font is JetBrains Mono. The layer is hidden on mobile. Smooth scroll is Lenis with `lerp:.1`. This is the closest match in tone to 42 (mono, terminal, brackets).

**B. Fiddle.Digital: velocity-skew cursor, difference dot, aim corners, canvas grid.** Built on their own StringTune library. Every frame it writes four CSS variables on `.string-cursor`: `--x`, `--y`, `--x-lerp`, `--y-lerp`, where the lerp values are smoothed velocity. All the motion is in CSS: `transform: translate3d(calc(1px*var(--x) + 1rem), calc(1px*var(--y) + 1.5rem),0) skew(clamp(-30deg, -1deg*var(--x-lerp), 30deg), clamp(-30deg, -1deg*var(--y-lerp), 30deg))`, so the cursor shears in the direction of travel and settles back when it stops. `.cursor-circle` is 20px with `mix-blend-mode:difference`. Its `::before` is a 1rem white dot that scales from 0 to 1 over 1.2s `cubic-bezier(.22,.31,0,1)` when you hover a link. Its `::after` is a 0.8rem ring made with the mask content-box trick. An `@property --a` angle animates a conic "loading" sweep (`cursorLoading` keyframes). Targets opt in with `string="cursor" string-cursor-class="-project-route|-project-blank|-circle|-all"`, which swaps between an "enter" icon and an "external" icon. Project previews follow the cursor with a parallax of `translate3d(clamp(±2rem, vx*-.025rem), ...)`. A fixed `.aim` frame with `tl/tr/bl/br` corner spans uses `mix-blend-mode:exclusion`; these crosshair corners are the second ingredient of the chosen effect. The "Canvas grid" entry is a set of `<canvas string="cursor">` layers (`pointer-events:all`) over the media. From the tags (canvas, webgl, grid, icons), the cells near the cursor react. The whole cursor is hidden below 1024px.

**C. Petra Garmon (fil studio): state label pill with idle fade.** `#cursor` is a fixed 0x0 box with `will-change:transform` and a translate matrix. It holds a pill (1px border, translucent background, 1rem radius, 1.5rem x 2.5rem padding) whose content changes with a `cursor-state` attribute: `scroll` shows a rotated arrow, `view-link` and `custom-external` become round with a -45deg arrow, and `scroll-progress` fills with `scaleX(var(--timer-progress))`. When the pointer is idle, `.cursor-still .cursor-still-hidden` fades out after a 2s delay (`opacity .5s cubic-bezier(.45,0,.55,1) 2s`). A WebGL duotone glitch layer sits behind. The cursor is disabled in portrait.

**D. Amaterasu (Exo Ape): ring that draws itself, with a CSS-transition lag.** `.cursor` is 80px (5.556vw) and fixed. It lags because of a CSS transition (`transform 1s cubic-bezier(.215,.61,.355,1)`), not a rAF lerp, which gives a soft "heavy" trail. An inner element gets the raw `translate3d`. The SVG circle (r 97.5, `vector-effect:non-scaling-stroke`) draws on by animating `stroke-dashoffset`. The `is-arrow` and `is-label` states grow it to 6.94vw, and `is-light` swaps its colour over pale sections. Separately, a WebGL point cloud is displaced around the mouse. Hidden at 600px and below.

**E. Stas Bondar: pill-to-panel morph.** The native cursor is `crosshair`. Hovering the awards list shows `.awards_cursor`, a 105x35 dark pill reading "FULL LIST". On click it morphs into a 490px by half-viewport preview panel by transitioning width, height, border-radius and background over `.6s cubic-bezier(.84,0,.16,1)`; the label fades out after a 0.15s delay. The accent red is #eb4330. Note that this animates layout properties, so it is not compositor-only.

**F. Ryan Strzok: ring with velocity skew.** `#cursor` is a fixed 54x54 SVG circle at z-index 102. Its transform matrix carries a small skew taken from velocity, the same idea as Fiddle's. A full-screen `#exclusion` layer and WebGL tiles add the extras.

**G. Ideology: GSAP cursor, text scramble, image trail.** Built with GSAP, ScrollTrigger and a `scramble.js` that cycles the characters of a hover label before it settles. A `mouse-images` script drops images along the mouse path, a trail that fades. Digit scramble is the "data" flourish I reuse for chart readouts.

**H. Wodniack: red contrast mask.** `.site-contrast-mask` is a fixed full-screen `#f40c3f` layer with `mix-blend-mode:darken` and a 1rem `#160000` edge, swept from `translate3d(-100%,0,0)` to 0. It is the best example of a deep red undertone on near-black using blend modes alone.

**I. Radiance: label pill and physics letters.** `.portfolio-cursor` is an 80x40 pill reading "SHOW" (radius 12px, background #060606) moved by transform. The header uses `mix-blend-mode:difference`, and Matter.js letters collide with the pointer.

**J. Eva Sanchez: shader hover.** GSAP plus about ten WebGL canvases (`glF` for type, `glMedia` for images) run fractal and ripple distortion shaders on hover. A fixed `.mouse` element tracks the pointer. It is beautiful, but heavy and wrong for data you have to read.

## 3. Choice for 42: "Target Lock", a reticle that snaps to targets plus a bracketed readout

It combines **Fiddle.Digital's corner-aim frame with velocity lag** (B) and **Super Evil Genius Corp's bracketed mono label** (A), with Ideology's digit scramble (G) for the numbers.

Why it fits 42: 42's frames already carry `+` crosshair corners (`.frame::before/::after`), its numerals are mono, and its charts are day tapes. A reticle made of four independent L-corners trails the pointer as a small 28px sight. When it reaches a card, button or chart column, the corners fly out and lock onto the target's bounds, so the page feels like an instrument acquiring a target. Over a day cell it reads out `[ MON 12 OCT · 14 ]` on the date axis under the column, with the digits resolving like a counter. Every moving part is a transform, so it costs almost nothing. It never covers data, and it keeps the native cursor. I rejected WebGL fluid, image trails and distortion because they compete with the data and add GPU load.

### DOM
One layer is injected at the end of `<body>`, with `aria-hidden="true"`:
```
div.rx            fixed inset:0, pointer-events:none, z-index 9000
  div.glow        480px radial red glow, screen blend
  i x4            L-corners, 10x10px, 1.5px stroke
  div.tag         mono readout "[ ... ]"
```
Markup hooks: `data-cursor="Open evidence"` on any element sets its label. Elements with `.tc` (day cells) are picked up automatically, and their `title` becomes the readout.

### States
| State | Trigger | Corners | Tag | Glow |
|---|---|---|---|---|
| Default | anywhere else | 28px box around the pointer; staggered lerp 0.34/0.27/0.21/0.27 so the box shears with motion (Fiddle skew without a skew) | hidden | follows, lerp 0.12 |
| Link or button | `a, button, [role=tab], [data-cursor]` | lock to the element rect, outset 4px, lerp 0.22, red drop-shadow glow | `data-cursor` text if present, 18px right and 22px below the pointer | follows |
| Card or frame | `.card, .frame` (ignored if larger than 70% of the viewport) | lock to the rect, outset 8px | `data-cursor` if present | follows |
| Chart cell | `.tc` | lock to the column, outset 2px (top and bottom corners meet as brackets) | `title` text, centred 4px below the column over the date axis (which carries the same date), clamped to the viewport, digits scramble for 180ms. Not placed above, because that covers the card title | follows |
| Text or input | `input, textarea, select, [contenteditable]`, prose `p, li, h1-h3` outside frames | fade out (opacity .15s) so glyphs stay clear; native I-beam | hidden | follows |
| Pressed | pointerdown | outset tightens by 3px (or the box by 4px) through the same lerp, so it reads as a click with no timeline | unchanged | unchanged |
| Leaving window | `mouseleave` on `<html>`, window blur | whole layer fades out over 200ms; snaps back to the pointer on return so nothing flies in | | |
| Keyboard | Tab or arrow keydown | layer hidden (`html.rx-kb`) until the next mouse move | | |

Optional chart focus (pure CSS, already in the reference): `.cells:hover .tc:not(:hover) .bar{opacity:.45}` dims the other days.

### Motion
- One rAF loop. Lerp is frame-rate independent: `a = 1 - (1-k)^(dt/16.67)`, with dt capped at 64ms.
- The loop stops once every delta is under 0.2px, so an idle page uses no CPU. It restarts on pointermove, scroll (capture, passive), pointerdown and pointerup.
- Target rects come from `getBoundingClientRect()` inside the loop. Only transforms are written, so there is no layout thrash, and scrolling while locked follows the target. On scroll the element under the pointer is re-checked with `elementFromPoint`.
- Opacity transitions: layer 200ms, corners 150ms, tag 160ms, all `ease`. Corner colour and glow transition over 140ms (`var(--t-fast)`).

### Colour (Ogilvy red undertone)
- Corners `var(--brand, #E41424)` at full strength, with `filter: drop-shadow(0 0 4px rgba(228,20,36,.7))` once locked. The corner layers are static, so they rasterise once.
- Tag: text #F5EDEE, background `rgba(20,6,8,.88)` (near-black with a red cast), border `rgba(228,20,36,.6)`, brackets in brand red.
- Glow: `radial-gradient(closest-side, rgba(228,20,36,.16), rgba(228,20,36,.05) 55%, transparent)` with `mix-blend-mode:screen`, so it warms the near-black to a dark red undertone as you move. In the light theme use `multiply` at 50% opacity, or drop the glow.

### Performance
- Compositor only: `translate3d` and opacity, with `will-change:transform` on the six moving nodes. No width, height, left or top animation. No canvas, no WebGL, no library.
- Gated by `(hover:hover) and (pointer:fine) and (prefers-reduced-motion:no-preference)` in both CSS and JS, and pointer events with `pointerType!=='mouse'` are ignored. Touch and pen get nothing.

### Accessibility
- The native cursor is never hidden. The layer is `aria-hidden` and `pointer-events:none`.
- Focus rings stay untouched, and the layer hides as soon as the keyboard is in use.
- With `prefers-reduced-motion: reduce` the script exits and only the native cursor remains.
- The layer never covers data: it fades out over text, the chart readout sits on the date axis under the column rather than on the bars or the card title, and the corners sit outside the target bounds.
- The `title` is moved to `data-tip-t` only while hovered (to avoid a double tooltip) and is restored on leave. The tape's `role="img"` `aria-label` still carries the data for screen readers.

### Avoid
- `cursor:none`: it loses precision on 3px-gap chart cells.
- `mix-blend-mode:difference` on the corners: it inverts red to cyan on the light theme.
- Animating `width`/`height` for the lock, as Stas Bondar does.
- WebGL, fluid or trails over charts. Labels at the pointer over charts. Scramble longer than 200ms.
- Running the loop forever, and `transition: transform` on the moving nodes (it fights the lerp).

## 4. Reference implementation (under 120 lines, paste before `</body>`)

```html
<style>
@media (hover:hover) and (pointer:fine) and (prefers-reduced-motion:no-preference){
.rx{position:fixed;inset:0;pointer-events:none;z-index:9000;opacity:0;transition:opacity .2s ease;contain:strict}
.rx.on{opacity:1}html.rx-kb .rx{opacity:0}
.rx i{position:absolute;left:0;top:0;width:10px;height:10px;border:0 solid var(--brand,#E41424);will-change:transform;
  transition:opacity .15s ease,filter .14s ease}
.rx i:nth-of-type(1){border-width:1.5px 0 0 1.5px}.rx i:nth-of-type(2){border-width:1.5px 1.5px 0 0}
.rx i:nth-of-type(3){border-width:0 1.5px 1.5px 0}.rx i:nth-of-type(4){border-width:0 0 1.5px 1.5px}
.rx.lock i{filter:drop-shadow(0 0 4px rgba(228,20,36,.7))}.rx.text i{opacity:0}
.rx .glow{position:absolute;left:-240px;top:-240px;width:480px;height:480px;border-radius:50%;will-change:transform;
  background:radial-gradient(closest-side,rgba(228,20,36,.16),rgba(228,20,36,.05) 55%,transparent);mix-blend-mode:screen}
[data-theme=light] .rx .glow{mix-blend-mode:multiply;opacity:.5}
.rx .tag{position:absolute;left:0;top:0;font:500 11px/1 var(--mono,ui-monospace,monospace);letter-spacing:.08em;
  text-transform:uppercase;color:#F5EDEE;background:rgba(20,6,8,.88);border:1px solid rgba(228,20,36,.6);
  padding:6px 8px;white-space:nowrap;opacity:0;transition:opacity .16s ease;will-change:transform;font-variant-numeric:tabular-nums}
.rx .tag.on{opacity:1}.rx .tag::before{content:"[ ";color:var(--brand,#E41424)}.rx .tag::after{content:" ]";color:var(--brand,#E41424)}
.cells:hover .tc:not(:hover) .bar{opacity:.45;transition:opacity .14s ease}
}
</style>
<script>
(()=>{
const mq=matchMedia('(hover:hover) and (pointer:fine) and (prefers-reduced-motion:no-preference)');
if(!mq.matches)return;
const R=document.createElement('div');R.className='rx';R.setAttribute('aria-hidden','true');
R.innerHTML='<div class="glow"></div><i></i><i></i><i></i><i></i><div class="tag"></div>';
document.body.append(R);
const glow=R.firstChild,C=[...R.querySelectorAll('i')],tag=R.querySelector('.tag'),H=document.documentElement;
const S=10,K=[.34,.27,.21,.27];
let px=-99,py=-99,gx=0,gy=0,tx=0,ty=0,tw=0,target=null,kind='',down=false,raf=0,last=0,scr=0;
const cp=C.map(()=>({x:0,y:0}));
function classify(el){
  if(!el||!el.closest)return['',null];
  let t=el.closest('input,textarea,select,[contenteditable]');if(t)return['text',t];
  t=el.closest('.tc');if(t)return['cell',t];
  t=el.closest('[data-cursor],a,button,[role=tab]');if(t)return[t.matches('.card')?'card':'hit',t];
  t=el.closest('.card,.frame');
  if(t){const b=t.getBoundingClientRect();if(b.width*b.height<.7*innerWidth*innerHeight)return['card',t]}
  t=el.closest('p,li,h1,h2,h3');if(t)return['text',t];
  return['',null];
}
function setTag(s){
  cancelAnimationFrame(scr);
  if(!s){tag.classList.remove('on');return}
  tag.textContent=s;tw=tag.offsetWidth;tag.classList.add('on');
  const t0=performance.now(),step=n=>{const p=(n-t0)/180;
    tag.textContent=p>=1?s:s.replace(/\d/g,()=>Math.random()*10|0);if(p<1)scr=requestAnimationFrame(step)};
  scr=requestAnimationFrame(step);
}
function setTarget(k,t){
  if(t===target)return;
  if(target&&target.dataset.tipT!==undefined){target.title=target.dataset.tipT;delete target.dataset.tipT}
  target=t;kind=t?k:'';
  if(k==='cell'&&t.title){t.dataset.tipT=t.title;t.removeAttribute('title')}
  setTag(k==='cell'?(t.dataset.tipT||''):(t&&k!=='text'&&t.dataset.cursor)||'');
  R.classList.toggle('lock',k==='hit'||k==='card'||k==='cell');R.classList.toggle('text',k==='text');wake();
}
function wake(){if(!raf){last=performance.now();raf=requestAnimationFrame(tick)}}
const ease=(k,dt)=>1-Math.pow(1-k,dt);
function tick(now){
  const dt=Math.min(64,now-last)/16.67;last=now;let moving=false,r;
  if(target&&!target.isConnected)setTarget('',null);
  const locked=target&&kind!=='text';
  if(locked){const b=target.getBoundingClientRect(),o=(kind==='card'?8:kind==='cell'?2:4)-(down?3:0);
    r=[b.left-o,b.top-o,b.right+o,b.bottom+o]}
  else{const h=14-(down?4:0);r=[px-h,py-h,px+h,py+h]}
  const P=[[r[0],r[1]],[r[2]-S,r[1]],[r[2]-S,r[3]-S],[r[0],r[3]-S]];
  C.forEach((c,i)=>{const a=ease(locked?.22:K[i],dt),p=cp[i],dx=P[i][0]-p.x,dy=P[i][1]-p.y;
    p.x+=dx*a;p.y+=dy*a;if(Math.abs(dx)+Math.abs(dy)>.2)moving=true;
    c.style.transform=`translate3d(${p.x}px,${p.y}px,0)`});
  const ga=ease(.12,dt);gx+=(px-gx)*ga;gy+=(py-gy)*ga;if(Math.abs(px-gx)+Math.abs(py-gy)>.2)moving=true;
  glow.style.transform=`translate3d(${gx}px,${gy}px,0)`;
  let lx=kind==='cell'?(r[0]+r[2])/2-tw/2:px+18,ly=kind==='cell'?r[3]+4:py+22;
  lx=Math.max(8,Math.min(innerWidth-tw-8,lx));ly=Math.max(8,ly);
  const ta=ease(.4,dt);tx+=(lx-tx)*ta;ty+=(ly-ty)*ta;if(Math.abs(lx-tx)+Math.abs(ly-ty)>.2)moving=true;
  tag.style.transform=`translate3d(${tx}px,${ty}px,0)`;
  raf=moving?requestAnimationFrame(tick):0;
}
addEventListener('pointermove',e=>{
  if(e.pointerType!=='mouse')return;px=e.clientX;py=e.clientY;
  if(!R.classList.contains('on')){cp.forEach(p=>{p.x=px;p.y=py});gx=tx=px;gy=ty=py;R.classList.add('on')}
  H.classList.remove('rx-kb');const[k,t]=classify(e.target);setTarget(k,t);wake();
},{passive:true});
addEventListener('pointerdown',e=>{if(e.pointerType==='mouse'){down=true;wake()}},{passive:true});
addEventListener('pointerup',()=>{down=false;wake()},{passive:true});
addEventListener('scroll',()=>{if(px<0)return;const[k,t]=classify(document.elementFromPoint(px,py));setTarget(k,t);wake()},{passive:true,capture:true});
H.addEventListener('mouseleave',()=>{R.classList.remove('on');setTarget('',null)});
addEventListener('blur',()=>{R.classList.remove('on');setTarget('',null)});
addEventListener('keydown',e=>{if(e.key==='Tab'||e.key.startsWith('Arrow'))H.classList.add('rx-kb')});
mq.addEventListener('change',()=>{if(!mq.matches){R.remove();setTarget('',null)}});
})();
</script>
```

Integration notes for 42 (`docs/gamma/prototype/gamma-prototype.html`):
- Day cells already render as `<span class="tc ..." title="Mon 12 Oct: 14">`, so the readout works without changes. A clearer title is `MON 12 OCT · 14 POSTS`.
- Add `data-cursor="Open evidence"` to the card buttons and `data-cursor="Switch market"` to the tabs as needed.
- `--brand` is #E41424 in the light theme and #FF3B47 in the dark theme. To keep Ogilvy red exactly, set `.rx{--brand:#E41424}`.
