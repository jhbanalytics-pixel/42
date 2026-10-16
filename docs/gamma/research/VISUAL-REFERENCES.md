# 42: Visual references and design moves (research, 16 October 2026)

Purpose: give the 42 redesign a grounded visual direction. The product owner supplied six sources. I read each one, picked the sites most relevant to a dark, data-dense, daily intelligence tool, and visited every pick in a real Chromium browser (Playwright, 1440x900 desktop, plus 390x844 mobile). For each site I captured computed styles straight from the live DOM: loaded font families, the font sizes and letter-spacing in use, text and background colours, transition durations and easings, CSS animations, backdrop filters, blend modes, canvas and video counts, and how many `prefers-reduced-motion` rules the stylesheets contain. Where a value below is quoted as hex, rgb or oklch, it came from the live CSS, not from guessing at a screenshot.

Screenshots are in the session scratchpad at `/tmp/claude-0/-home-user-42/5c5ff21d-b70b-5237-8c1e-a52d1e61acf3/scratchpad/refs/`, named `<site>.png` (desktop), `<site>-mobile.png` and, for some, `<site>-scrolled.png`. That folder is not on the shared drive; copy it across if the team needs the images.

Status words used below: **verified** means I opened the live site and measured it. **Unverified** means I could not open it, so anything said about it comes only from the source article.

---

## 1. Sources and what each one lists

| # | Source | Opened? | What it is | Count |
|---|---|---|---|---|
| 1 | https://fireart.studio/blog/20-best-futuristic-website-examples-to-inspire-you/ | Yes | Screenshot gallery with links, no commentary | 20 sites |
| 2 | https://www.smartwebagency.co.uk/10-creative-websites-to-inspire-your-next-web-design-in-2026/ | **No, unverified** | Blocked by a Cloudflare bot challenge (HTTP 403 to WebFetch and curl, challenge loop in headless Chromium, the Wayback Machine copy also served the challenge, and a web search did not surface its contents) | Unknown (title says 10) |
| 3 | https://onepagelove.com/style/futuristic | Yes, all 6 pages | Curated one-page sites tagged "futuristic" | 47 (header count) |
| 4 | https://github.com/StanForever/awesome-websites (README read from `/master/`; `/main/` returns 404) | Yes | General "useful and fun websites" list, mostly utilities, not a design showcase | About 540 links |
| 5 | https://github.com/topics/immersive-web?l=typescript | Yes | GitHub topic page, TypeScript filter | 11 repos (36 across all languages) |
| 6 | https://www.eleken.co/blog-posts/best-website-design-examples | Yes | SaaS and product UX agency's annotated list | 50 sites (last ~6% of page text not parsed) |

### 1.1 Fireart Studio (20)
lusion.co, thedigitalpanda.com, ilabsolutions.it, martinehrlich.com, genesis.com, dottodot.es, asmobius.co.jp, krissrealestate.com, downtown-philly.com, loaf.agency, uptec.up.pt, feroxcorp.com, maman-corp.com, yauch.oscilloscope.net, sima-marmor.at, helloplayful.com, themakers.io, screenmedia.co.uk, creativepark.fr, voximplant.com.
Mostly agency reels and 3D showpieces. Studied: Lusion, Voximplant, UPTEC, Oscilloscope.

### 1.2 Smart Web Agency (unverified)
Not readable from this environment. No sites from it are included. If someone can open it in a normal browser, the list should be added.

### 1.3 One Page Love, "futuristic" style (pages 1 to 6)
Midnight (askmidnight.com), Spatial (spatial-festival.com), SVG Doodles, Aevion (drone.riotters.com), Elva (elvalabs.ai), Softle (softle.app), wild (craft.wild.as), Builders Table, Anatolii Grashchenko, Ascend, Digital Original XR, 2026 Generative AI Trends (Typeform AI data report), Jonathan Moore, Ozan Atmar, Shopify Editions Spring 2026, Vault AI (usevault.ai), Framer State of Sites 2026, They Call Me Giulio, Hardware Operations (hardwareoperations.com), Personal Computer by Perplexity, RiACT (riact.ai), Rig (rig.ai), Tenbin Labs (tenbinlabs.xyz), VOID (void2.art), Aaron Sananes, Fabric (fabric.studio), Hyve.systems, Mark My Images, Parable (parablevc.com), Refire, The Spark (spark.thedigitalpanda.com), Wope, 1Password Future, Blakeney, Crosswire, Eyep, Holographic Abstract 3D Backgrounds, Nabr, Unifi Protocol, Vue Graphics, Aristide Benoist, Blocksphere, Context AI, Disruptive By Design, Easy Rocket Studio, Tristan Farneau, You To The Future.
Studied: Midnight, Elva, Vault AI, Perplexity Personal Computer, Hardware Operations, Rig, Tenbin Labs, RiACT, Typeform AI report, Hyve.

### 1.4 awesome-websites
A utility directory (for example 10minutemail, builtwith, haveibeenpwned, regex and PDF tools), so most entries carry no design lesson. The data-visual entries are the useful ones: earth.nullschool.net, flightradar24.com, submarinecablemap.com, radio.garden, internetlivestats.com, stars.chromeexperiments.com, algorithm-visualizer.org, map.norsecorp.com, internet-map.net, dinosaurpictures.org/ancient-earth.
Studied: earth.nullschool, Flightradar24, Submarine Cable Map, Radio Garden.

### 1.5 Eleken (50)
UNITED24 Rebuild Map, HeyFriends!, Abetka UA, B-Egg Farm, Ellipsus, Mona Sans and Hubot Sans (GitHub), Apple Siri, Overrrides, Mode, Lusion, The Message to Ukraine, DIKO, Ventriloc, You Can Be Anything (TFTL), GRIDS (Obys), Depo Studio, Rebellion, Fabric VC, Hydra, StoryMakers Project, Auxility, SEBTO, Pebble Life, Can't Unsee, MORAL, прапор.укр, Fluent Health, Umbrel, Dumy, Enpower Trading, Flayks, The Web Can Do What!?, Crew by Tubik, SaveDay, Adidas Arena, Metadrop, Teenage Engineering, Art+Tech Report, Endless Tools, Ethnocare, Appwrite, Artbruno, Flying Papers, Arrow Dynamics, Pinnacle Design Lab, Kovalska, Tiny Wins, Gemnote, Stripe Press, Pitch.
Studied: UNITED24 Rebuild Map, Mode, Ventriloc, Enpower Trading (South African), Arrow Dynamics. Pinnacle Design Lab is **unverified**: the egress proxy refused the connection.

### 1.6 GitHub topic `immersive-web`, TypeScript (11 repos)
See section 5.

---

## 2. Sites studied (23 verified)

Selection rule: favour product sites, AI and data tools, data-heavy editorial and maps, and sci-fi interfaces with legible type. Pure 3D agency reels were included only where a technique transfers (Lusion, Oscilloscope).

### 2.1 Summary table

| Site | Source | Background | Display / text face | Mono | Accent | Motion signature | Reduced-motion rules |
|---|---|---|---|---|---|---|---|
| [Rig](https://rig.ai) | OPL | oklch(0.225 0.013 188) deep teal-black; hero in rgb(175,190,210)-ish blue | Chalet 700 (display), Instrument Sans 400 to 600 (UI) | Chivo Mono, Geist Pixel Square | oklch(0.60 0.166 254) blue, lilac #C4B2EC | Slow "dot-flow" loops 14 to 18 s, 1.1 s "thinking" dots, 0.35 s scene reveals | 5 |
| [Vault AI](https://www.usevault.ai) | OPL | #03020B near-black violet | Clash Display 600 (headings), Outfit 400/500 (UI) | none | #0A37E2 electric blue | 0.3 to 0.4 s cubic-bezier(0.4,0,0.2,1) | 0 |
| [Midnight](https://www.askmidnight.com) | OPL | #07172A ink navy, then #F4EFE6 paper | Public Sans 400/500 | none | warm paper | Named easing tokens: `--ease-smooth-out` cubic-bezier(.22,1,.36,1), `--digit-ease` cubic-bezier(.34,1.45,.64,1) | 8 |
| [Perplexity Personal Computer](https://www.perplexity.ai/personal-computer) | OPL | #FAF8F5 light, dark hero image | GT Canon VF (hero), pplxSerif, pplxSans | pplxSansMono | #20808D teal | 0.6 to 0.9 s cubic-bezier(0.22,1,0.36,1) and (0.16,1,0.3,1); 8 s "softFlow" | 78 |
| [Elva](https://elvalabs.ai/) | OPL | #131313 with film grain | Neue Haas Grotesk Display Pro 400 | none | white, #B3B3B3 secondary | 0.3 to 0.4 s, cubic-bezier(0.65,0,0.35,1); blurred type reveal | 1 |
| [Hardware Operations](https://hardwareoperations.com) | OPL | #E4E2DE warm grey | GT Pressura Extended (headings, -3% tracking) | Geist Mono 10 to 11 px, +0.06 to +0.08 em caps | #2CC956 status green, oklch(0.68 0.22 42) orange | 0.7 s cubic-bezier(0.16,1,0.3,1) | 0 |
| [Tenbin Labs](https://tenbinlabs.xyz/) | OPL | #000000 | PP Telegraf (86 to 173 px, -3% tracking) | Berkeley Mono | #CECAFB lavender | Full-screen preloader; never cleared in headless | 0 |
| [RiACT](https://www.riact.ai) | OPL | #0C0C0D | Faktum 400/500 | IBM Plex Mono 500 | #00F619 green, #FB3EFF magenta, #00D1FF cyan | 0.2 s ease, 0.4 s cubic-bezier(0.215,0.61,0.355,1) | 0 |
| [Typeform AI data report 2026](https://www.typeform.com/AI-data-report-2026) | OPL | #2A222B aubergine, #FAF9FB | TWK Lausanne 350/400/600, Tobias serif headings | Apercu Mono (declared) | #D258FE violet | 0.3 s ease-out; spectral bar hero | 0 |
| [Hyve.systems](https://www.hyve.systems/) | OPL | #171A1F, light #F6FAF9 | DM Sans 400/500, 93 px at -3% | DM Mono | #018D6B green | 0.8 s cubic-bezier(0.19,1,0.22,1); preloader stuck at "0%" in headless | 0 |
| [Lusion](https://lusion.co/) | Fireart, Eleken | #FFFFFF / #F0F1FA | Aeonik 400 | IBM Plex Mono, LusionMono | #1A2FFB blue, #C1FF00 lime | WebGL hero, 0.3 to 0.5 s cubic-bezier(0.4,0,0.1,1); 12-col grid `--grid-gap: 2vw` | 0 |
| [Voximplant](https://voximplant.com/) | Fireart | #000000 starfield | Geist 400/500 | Geist Mono | #05B493 teal, #8B5FFF violet | 0.77 s cubic-bezier(0.65,0,0.35,1); backdrop-blur(12px) cards | 6 |
| [UPTEC](https://uptec.up.pt/) | Fireart | #FFFFFF, #F2F2F2 | Foundry Monoline 700/800 caps, 12 px caps at -1% | none | #FFEEA4 pale yellow | 0.3 s cubic-bezier(0.4,0,0.2,1); `mix-blend-mode: multiply` on 218 elements | 1 |
| [Oscilloscope](https://yauch.oscilloscope.net/) | Fireart | dark photographic panorama | Helvetica Neue Bold, -4 px tracking | none | #FFCE00 | Drag-to-pan 360 panorama; 8 px caps labels | 0 |
| [earth.nullschool](https://earth.nullschool.net/) | awesome | #000005 | M PLUS 1 400 (13 to 16 px) | none | #E7CA7A gold for selected values | Animated particle flow on canvas; UI itself is static | 0 |
| [Flightradar24](https://www.flightradar24.com/) | awesome | #303030 panels over map | Open Sans, Roboto, Roboto Condensed | none | #F8C023 aircraft yellow | 0.15 s cubic-bezier(0.4,0,0.2,1); live positions | 2 |
| [Submarine Cable Map](https://www.submarinecablemap.com/) | awesome | #F9FAFB | Helvetica 16 px | none | per-cable categorical colours | 0.2 s linear; map plus searchable list | 0 |
| [Radio Garden](https://radio.garden/) | awesome | #2D00FF splash, #FFFFFF panels | Atlas Grotesk 400 | none | #00C864 green | Rotating globe; 0.18 to 0.36 s ease | 1 |
| [UNITED24 Rebuild Map](https://u24.gov.ua/uk/rebuild/map) | Eleken | #333333 / #000000 dark map | NuberNext, NuberNext Extended | none | #FFD601 yellow | 0.4 to 0.5 s ease; animated bar loader; backdrop-blur(40px) | 19 |
| [Mode](https://mode.com/) | Eleken | #043F2E deep green, #EEF2E3 | Grenette serif (96 to 173 px, -2%), Graphik 400/500 | none | #C8F169 lime | 0.25 to 0.35 s ease-in-out | 0 |
| [Ventriloc](https://ventriloc.ca/en/) | Eleken | #F5F5F5, #202020 | PolySans 400 (66 px at -2%), Inter 500 | none | #816729 brass, #FF682C orange, #6B1E1C bordeaux | Staggered fadeIn/translate 0.25 to 1.25 s; long preloader | 109 |
| [Enpower Trading](https://enpowertrading.co.za/) | Eleken | #F9F8F3 with fog texture | Arial (deliberately plain) at -2% | none | #EA2A10 red, #F2600C orange | 0.3 s cubic-bezier(0.4,0.4,0.1,1); `mix-blend-mode: multiply` on 50 elements | 0 |
| [Arrow Dynamics](https://arrowdynamics.dev/) | Eleken | #171818 / #0D0D0D | Instrument Serif 56 px, Inter 300 to 800 | Courier Prime | #A3EE79 phosphor green | Instrument-panel knobs, gauges; heavy blend modes; 36 canvases | 0 |

Every studied site reflowed to a 390 px viewport with no horizontal scroll (measured `scrollWidth` 375 to 390).

### 2.2 Notes per site (what transfers, what does not)

**Rig** (rig.ai). The single closest reference for 42. A dark product page that looks like an operations console: hairline 1 px frames around every panel, uppercase mono eyebrows at 12 to 12.8 px with +0.1 em tracking ("THE PROBLEM", "THE LOOP / 01"), a horizontal news ticker under the hero, a stepped "01 Hook / 02 Test / 03 Fix / 04 Retest" progress rail with pause and arrow controls, and a real product mock (code pane plus agent chat). Three type roles: Chalet for headlines (72 to 112 px, -3% tracking), Instrument Sans for running text, Chivo Mono for metadata and data. A pixel face (Geist Pixel Square) is used sparingly for status chips. Colours are written in oklch, which makes it easy to keep consistent lightness steps. Weak point: many mono labels sit at 9 to 10.4 px, which is too small for a daily tool. Transfer: the three-role type system, mono eyebrows, hairline panels, stepped rail with pause. Reject: sub-11 px labels.

**Vault AI** (usevault.ai). A finance dashboard sold on a dark violet-black page. Shows a real dashboard in the hero (balance "$76 192", line chart with a dashed forecast segment beyond "today"), feature cards with mini ledgers and "Connected / Pending" status pills. Corner crosshair marks ("+") at grid intersections give a drafting-table feel at zero cost. Text uses white at 60%, 50% and 40% opacity for hierarchy; 40% white on #03020B is roughly 3.7:1 and fails AA for body text. A large horizon-glow arc behind the hero is pure atmosphere. Transfer: dashed line for projected values, crosshair grid marks, status pills. Adapt: opacity-based text ramp (stop at 60%). Reject: the glow arc on working screens; no reduced-motion rules at all.

**Midnight** (askmidnight.com). Calm, not neon. Ink navy #07172A and paper #F4EFE6, Public Sans at 500 with tight negative tracking (-0.04 em at 43 to 48 px, -0.025 em at 15 to 26 px). The most disciplined motion system I found: named easing tokens in `:root`, including a dedicated `--digit-ease` (a slight overshoot for rolling numbers) and `--shimmer-ease` (linear, for loading shimmer). Text reveals word by word as you scroll, with not-yet-read words in a light tint. Eight reduced-motion rules. Transfer: easing tokens with names, a specific easing for number changes, a calm two-tone palette. Adapt: word-by-word reveal only for onboarding, never for data.

**Perplexity Personal Computer**. Glass taskbar UI floating over a photographic scene; serif (pplxSerif) headings against sans UI and a mono for commands. 78 reduced-motion rules, the most thorough of any product site here, which shows that rich motion and accessibility can coexist. Durations are long (0.6 to 0.9 s) with a strong ease-out-quint. Transfer: serif plus sans plus mono trio; strong ease-out; exhaustive reduced-motion coverage. Reject: photographic backgrounds behind data.

**Elva** (elvalabs.ai). Near-black #131313 with visible film grain, very large Neue Haas Grotesk Display (100 to 116 px), heavily blurred background type behind a sharp foreground phone mock. Backdrop blur plus `saturate(1.8)` on overlays. Transfer: grain as a low-cost surface texture (one tiny tiled PNG or SVG noise). Reject: the long black intro before any content appears (the first 9 s screenshot is blank).

**Hardware Operations** (hardwareoperations.com). Light, but the type system is pure "spec sheet": everything except two headlines is Geist Mono at 10 to 11 px, uppercase with +0.06 to +0.08 em tracking, section numbers like "01 / INFORMATION", a green "Online" status dot. Body grey #6A6862 on #E4E2DE is about 4.3:1, which fails AA at that size. Transfer: numbered section labels, status dot, extended display face for headers. Reject: mono for paragraphs, tiny grey text.

**Tenbin Labs** (tenbinlabs.xyz). Black page, PP Telegraf headlines up to 173 px, Berkeley Mono for data. Comparison stats are set as big pairs ("12secs vs 24hrs", "<3bps vs 40bps"). Its preloader never released in a headless browser after 18 s, so the screenshot shows only the logo ring; that is a real risk on slow connections and for bots. Transfer: paired comparison numerals. Reject: blocking preloaders.

**RiACT** (riact.ai). Dark #0C0C0D, Faktum sans with IBM Plex Mono for numbers and tags, signal colours green #00F619, magenta #FB3EFF and cyan #00D1FF used on small elements only. Large stats like "50 % Reduced Engineering Time" with the unit set smaller. Their video embed showed "We couldn't verify the security of your connection", a reminder that third-party embeds fail. Transfer: unit set smaller than the numeral; neon only as tiny signal marks. Reject: relying on embedded video.

**Typeform AI data report 2026**. The best data-editorial reference: TWK Lausanne for text, Tobias serif for pull quotes ("AI really affects everything I do at work now"), aubergine #2A222B with violet #D258FE. Hero is a spectral bar field that reads like a frequency chart. Transfer: editorial pull quotes in a serif next to data; a report rhythm of section label, big statement, chart, takeaway list. Reject: the purely decorative spectral hero.

**Hyve.systems**. DM Sans at 93 px with -3% tracking and a 0.8 line-height, DM Mono for labels. A percentage preloader ("0%") that never progressed in headless Chromium. Transfer: DM Sans plus DM Mono is a free, proven pairing. Reject: percentage preloaders.

**Lusion** (lusion.co). The 3D studio reference. Real-time WebGL hero (blue, black and white plastic shapes), a 12-column grid defined in CSS (`--grid-space: calc((100% - 11 * var(--grid-gap)) / 12)`, `--grid-gap: 2vw`), 20 px global radius, one house easing cubic-bezier(0.4,0,0.1,1). No reduced-motion rules. Transfer: the CSS grid variables and a single house easing. Reject: WebGL hero for 42.

**Voximplant** (voximplant.com). Black with a faint particle starfield, Geist and Geist Mono, glass cards with `backdrop-filter: blur(12px)`, coloured dot plus mono caption ("FOR DEVELOPERS", "FOR BUSINESS"). Transfer: dot plus mono caption as a category key; Geist pairing. Adapt: glass only on overlays that float above content.

**UPTEC** (uptec.up.pt). Brutalist monoline caps (Foundry Monoline), visible column rules, a rotating 3D cube logo, `multiply` blend on 218 elements. Transfer: visible column hairlines as structure. Reject: all-caps body copy (hurts reading speed and dyslexic readers).

**Oscilloscope** (yauch.oscilloscope.net). Drag-to-explore panorama with corner bracket frames and dotted arc connectors in #FFCE00. Navigation text is 8 px uppercase, far too small. Transfer: corner bracket frames to mark a focused item. Reject: drag-only navigation, 8 px labels.

**earth.nullschool**. The model for "data is the hero". Full-bleed canvas of animated wind particles on #000005, a tiny "earth" menu, M PLUS 1 at 13 to 16 px, selected values in gold #E7CA7A, grey #979797 secondary text (about 7:1, good). Animation is the data itself, not decoration. Transfer: motion only when it encodes data; one accent colour for "selected". Note it has no reduced-motion handling.

**Flightradar24**. Dense ops UI: ranked list with mono-like codes in pills (FAD458, A20N), counts aligned right, "LIVE" badge, bottom tool dock on mobile (Settings, Weather, Filters, Widgets, Playback). Transfer: ranked list with right-aligned counts, LIVE badge, a mobile bottom dock, a "Playback" time scrubber. Reject: ad panels and upsell clutter.

**Submarine Cable Map**. Map plus searchable list side by side, "Last updated Oct 13, 2026" printed on the map. On mobile the map stacks above the list. Transfer: always show data freshness; map and list as two views of one dataset.

**Radio Garden**. Single colour splash (#2D00FF) that gates audio behind "Press play to start". Transfer: one bold brand colour on the splash, never on data. Note: the gate is there for browser autoplay rules, a fair reason.

**UNITED24 Rebuild Map**. Dark satellite map, yellow #FFD601 accent, green cluster bubbles with counts, Map/List toggle, a running "TOTAL COLLECTED" figure in the header. 19 reduced-motion rules. The cookie banner covers almost half the screen on desktop and the entire mobile screen. Transfer: Map/List toggle, cluster counts, a single headline total. Reject: heavy consent walls over the product.

**Mode** (mode.com). A BI company using a warm serif (Grenette) with Graphik, deep green #043F2E with lime #C8F169, mini candlestick chart in the hero card. Shows a data product does not need to look cold. Transfer: confident serif display for editorial moments; a two-colour brand pair with one high-energy accent. Reject: speech-bubble shapes around content.

**Ventriloc** (ventriloc.ca). Data consultancy, PolySans plus Inter, earthy brass #816729, orange #FF682C and bordeaux #6B1E1C. 109 reduced-motion rules and staggered entrance animations, but a long full-screen brass preloader blocked both screenshots. Transfer: earthy, warm palette as an alternative to blue neon. Reject: preloader.

**Enpower Trading** (enpowertrading.co.za, South Africa). Plain Arial at -2% tracking on a fog texture, red #EA2A10 and orange #F2600C, `multiply` blending on 50 elements, bracketed index codes like "[00-1]", an "Energy usage: Med" control. Shows that a local energy brand can feel futuristic through layout and restraint rather than effects. Transfer: bracketed index codes, a mood texture that stays very low contrast. Adapt: swap Arial for a proper grotesk.

**Arrow Dynamics** (arrowdynamics.dev). Skeuomorphic instrument panel: knobs, a dial gauge, phosphor green #A3EE79, Instrument Serif headings, Courier Prime mono, 9 to 10 px labels at +0.2 em tracking, 36 canvases and 156 overlay blend elements. Transfer: a single gauge-like "confidence dial" idea, phosphor green as a status colour. Reject: skeuomorphic knobs, heavy blend stacks, canvas-per-widget.

---

## 3. Synthesis: recurring "futuristic" design moves in 2025 to 2026, and verdicts for 42

| # | Move | Seen on | Verdict | Reason for 42 |
|---|---|---|---|---|
| 1 | **Three type roles: grotesk for UI, mono for data and metadata, a display or serif voice for headlines** | Rig, Perplexity, Hardware Operations, RiACT, Voximplant, Hyve, Typeform, Mode, Arrow Dynamics, Tenbin | **Adopt** | Mono with tabular figures makes counts, dates and confidence values line up and read as "instrument". The display face gives the daily brief its editorial voice. Keep it to three families. |
| 2 | **Uppercase mono eyebrows and numbered section labels** ("01 / INFORMATION", "THE LOOP / 01", "[00-1]") | Rig, Hardware Operations, Vault AI, Voximplant, Enpower | **Adopt** | Cheap, scannable wayfinding for a dense screen. Set at 12 px minimum with +0.06 to +0.1 em tracking; never smaller than 11 px. |
| 3 | **Hairline frames, visible grid and crosshair marks** (1 px borders at 8 to 18% white, corner "+" marks, column rules) | Rig, Vault AI, UPTEC, Oscilloscope (brackets), Lusion (CSS grid) | **Adopt** | Structure without heavy cards; works in dark mode and costs nothing. Hairlines are decorative, so they do not need 3:1, but interactive boundaries do. |
| 4 | **Near-black tinted backgrounds, not pure black** (#07172A navy, #03020B violet, oklch(0.225 0.013 188) teal, #0C0C0D) | Midnight, Vault AI, Rig, RiACT, Elva, Arrow Dynamics | **Adopt** | Reduces halation and gives a brand tint. Define the ramp in oklch so lightness steps stay even, and check every text step for 4.5:1. |
| 5 | **One neon or high-chroma signal accent on an otherwise quiet palette** (lime, phosphor green, electric blue, magenta) | RiACT, Arrow Dynamics, Mode, Lusion, Vault AI, UNITED24 (yellow) | **Adapt** | Use one accent for "new or rising today" and selection. Do not encode categories with neon; use a validated categorical palette and pair colour with a shape or label (colour-blind safety). |
| 6 | **Glass panels** (backdrop-filter blur 5 to 40 px, often with saturate) | Voximplant, Perplexity, Elva, Midnight, UNITED24, Rig | **Adapt** | Only for floating layers (sheet, popover, sticky header). Never behind charts or long text. Provide a solid fallback with `@supports` and for `prefers-reduced-transparency`. Blur is expensive on low-end Android, which matters in our markets. |
| 7 | **Grain, fog or noise texture** | Elva, Enpower Trading, Typeform (spectral) | **Adapt** | A 2 to 4% opacity tiled noise on the page background adds depth for a few kilobytes. Never on chart areas, and keep it static. |
| 8 | **Glow arcs, starfields, particle fields and spectral heroes** | Vault AI, Voximplant, Typeform, Tenbin | **Reject** for working screens | Decorative motion and glow compete with data and burn battery. A single static glow behind the logo on the sign-in screen is the most it should get. |
| 9 | **Motion that encodes data** (wind particles, live aircraft positions, rolling digits, stepped progress rail) | earth.nullschool, Flightradar24, Midnight (`--digit-ease`), Rig (01 to 04 rail) | **Adopt** | Animate a change, not a page. Count-up or roll on a number when a value updates (200 to 400 ms), a brief highlight on newly arrived rows, a stepped rail for the daily pipeline. All of it off under `prefers-reduced-motion`. |
| 10 | **Named easing tokens with long, strong ease-out** (cubic-bezier(.22,1,.36,1) and (.16,1,.3,1), 0.25 to 0.9 s) | Midnight, Perplexity, Hardware Operations, Hyve, Lusion | **Adopt** | Gives the UI a single "feel". For a tool, shorten: 120 to 200 ms for hover and press, 240 to 320 ms for panels, nothing over 400 ms. |
| 11 | **Scroll-driven reveals and word-by-word text** | Midnight, Ventriloc, Elva, Typeform, Lusion | **Reject** for data, **adapt** for onboarding | A strategist opening 42 daily must see everything at once. Content hidden until scrolled also hides it from print, screenshots and some screen readers. |
| 12 | **Preloaders and intro sequences** | Hyve, Tenbin, Ventriloc, Elva, Arrow Dynamics | **Reject** | Three of these never cleared in a headless browser within 18 s. Use skeletons that match the final layout, and render cached data instantly. |
| 13 | **Big editorial numerals with smaller units, and paired comparisons** ("50 %", "12secs vs 24hrs", "$76 192") | RiACT, Tenbin, Vault AI, UNITED24 total | **Adopt** | Ideal for the daily headline numbers. Always show the uncertainty next to the number (range or band), and print freshness ("Updated 06:00 SAST") as Submarine Cable Map does. |
| 14 | **Map and list as two views of one dataset, with a bottom dock on mobile** | UNITED24, Submarine Cable Map, Flightradar24, Radio Garden | **Adopt** | Fits three markets (South Africa, Nigeria, Kenya): a market switcher plus a list view that is the accessible default; the map or chart is the enhancement. |

### 3.1 How the moves map to 42's honesty rules

- **Uncertainty**: take Vault AI's dashed forecast segment and generalise it. Solid line for observed, dashed for modelled, a translucent band for the interval. Mono labels state the interval in words ("68 to 81, medium confidence") so it is not colour-only.
- **Missing is not zero**: none of the studied sites draw missing data well, so this is where 42 can lead. Use a hatched or dotted "no data" fill with a mono "NO DATA" label, a gap in lines (never a drop to zero), and an empty-state cell that differs in pattern, not only in tint.
- **Freshness**: a "LIVE" or "Updated" chip in the header, as Flightradar24 and Submarine Cable Map do.

---

## 4. Recommended freely licensed fonts (Google Fonts, SIL Open Font License)

| Pairing | Why it delivers the look | Seen on |
|---|---|---|
| **Geist + Geist Mono** (Vercel, on Google Fonts) | Neutral, technical grotesk with a matching mono drawn on the same skeleton, so numbers in mono and labels in sans feel like one family. Variable weights 100 to 900. Tabular figures available. | Voximplant (Geist, Geist Mono), Hardware Operations (Geist Mono), Tenbin (Geist) |
| **Instrument Sans + JetBrains Mono (or Chivo Mono)**, with **Instrument Serif** for occasional editorial headlines | Instrument Sans is slightly condensed and crisp at small sizes, good for dense tables. Instrument Serif gives the "daily brief" an editorial voice like Typeform and Mode. Chivo Mono is what Rig uses; JetBrains Mono has excellent digit distinction (0 versus O, 1 versus l). | Rig (Instrument Sans, Chivo Mono), Arrow Dynamics (Instrument Serif) |
| **IBM Plex Sans + IBM Plex Mono** | The safest choice for broad language and character coverage, very clear numerals, long-proven in data products. Less fashionable, more dependable. | RiACT (Plex Mono), Lusion (Plex Mono loaded) |

Fallback mention: DM Sans + DM Mono (Hyve) and Public Sans (Midnight) are also free and proven, but less distinctive.

Recommended for 42: **Geist + Geist Mono** as the system, with **Instrument Serif** reserved for the one daily headline and pull quotes. Load only the weights used (for example Geist 400, 500, 600; Geist Mono 400, 500) as WOFF2 with `font-display: swap`, and turn on `font-variant-numeric: tabular-nums` for every data cell.

---

## 5. Immersive web (GitHub topic `immersive-web`, TypeScript)

### 5.1 Repositories listed (11)

| Repo | Stars | What it is | Stack (from topics) |
|---|---|---|---|
| zlbigger/immersive-poetry-page | 73 | A Codex skill that turns classical Chinese poetry into narrated, scroll-driven pages | Vite, image generation |
| WonderlandEngine/wonderbricks | 4 | Calm brick-building WebXR showcase | Wonderland Engine, WebXR, WebGL, Meta Quest |
| jackiectl/dim-sum-parlor | 3 | Walkable 3D portfolio with a CRT desktop | three.js, CSS3D, WebGL |
| huanleya/britain-an-immersive-journey | 1 | Interactive web documentary | React, three.js, Tailwind, Vite |
| brenychstudio/brenychstudio | 1 | Studio site with immersive routes and SEO structure | React, three.js, framer-motion, Tailwind, Cloudflare Pages |
| yashwanth07-debug/almost | 0 | Real-time generated 3D museum | three.js, react-three-fiber, GLSL |
| naveenk7100-ship-it/arun-tattoos | 0 | 3D studio site with animated model | React, three.js, react-three-fiber, framer-motion |
| rxshii94x/reveal-future | 0 | Scroll-driven digital archive | React, storytelling motion |
| IBF/void-realms-AI | 0 | 3D landing page for a self-hosted AI advisor | Next.js, three.js, react-three-fiber, GSAP ScrollTrigger, particle system, framer-motion; tagged a11y |
| brenychstudio/4walls | 0 | Branching cinematic narrative with an optional XR scene | React, WebXR |
| victor-kiss/portfolio-ui | 0 | Portfolio UI | Next.js, three.js, react-three-fiber |

The topic is small and dominated by portfolios. No repo here is a data-visualisation library. The useful signal is which building blocks recur: three.js, react-three-fiber, GSAP ScrollTrigger, framer-motion, GLSL shaders, WebXR.

### 5.2 What could transfer to a daily data tool, at acceptable cost

Size figures below are approximate, minified and gzipped, from general knowledge of these libraries; they were not measured in this session.

| Technique | Cost | Verdict | Conditions |
|---|---|---|---|
| A single small **WebGL or canvas "signal field"** on the landing or sign-in screen only (for example a slow particle drift built from the day's trend count) | three.js core is roughly 150 kB or more gzipped; a hand-written 2D canvas or tiny WebGL shader can be under 5 kB | **Adapt, with a hand-rolled canvas, not three.js** | Lazy-load after first paint; pause when the tab is hidden; static image under `prefers-reduced-motion`; skip on `navigator.connection.saveData`; `aria-hidden`. |
| **CSS-only scroll-driven animation** (`animation-timeline: view()`) for a progress rail or section fade | Zero JavaScript | **Adapt** | Only for non-essential polish; content must be visible without it; wrap in `@supports` and the reduced-motion query. |
| **framer-motion / Motion** for layout transitions (list reordering when ranks change) | Full library roughly 30 to 50 kB; the "mini" animate API is a few kB | **Adapt** | Use the small API or the View Transitions API, which is native and free. Rank changes animating into place is genuinely informative. |
| **GSAP ScrollTrigger** pinned storytelling | Roughly 25 kB core plus about 12 kB plugin | **Reject** for the app; possible for a one-off public report page | Pinning fights native scroll, breaks keyboard and screen-reader reading order, and is hard on mobile. |
| **react-three-fiber 3D scenes** (globes, rooms, product models) | three.js plus R3F plus drei can pass 250 kB; constant GPU use | **Reject** | 3D adds no truth to trend data, drains battery on mid-range Android, and has no accessible equivalent. A flat SVG map of the three markets does the job. |
| **GLSL shader backgrounds**, particle systems, film-grain shaders | GPU every frame | **Reject**; use a static noise PNG or SVG instead | Same look for a few kilobytes and no GPU. |
| **WebXR** | Device-specific | **Reject** | No user need; strategists work on laptops and phones. |
| **Optional immersive route** pattern (4walls: an XR scene you opt into) | Code-split, so near zero for normal users | **Adapt the principle** | If 42 ever has a "showcase" view for pitches, it must be a separate, opt-in route that never loads in the daily app. |

Rule of thumb for 42: every animation must either encode data or confirm an action; total motion and effects JavaScript under about 15 kB gzipped; nothing runs on the GPU continuously; everything has a reduced-motion and a low-data path.

---

## 6. Accessibility and performance warnings drawn from the evidence

- **Reduced motion is mostly ignored.** 13 of 23 sites had zero `prefers-reduced-motion` rules (Vault AI, Tenbin, Lusion, Hyve, RiACT, Typeform, Mode, Enpower, Arrow Dynamics, earth.nullschool, Submarine Cable Map, Oscilloscope, Hardware Operations). The best examples are Perplexity (78), Ventriloc (109) and UNITED24 (19). 42 should match the best.
- **Tiny labels are endemic** in the "futuristic" look: 8 px (Oscilloscope), 9 to 10 px (Rig, Arrow Dynamics, Hardware Operations). 42 minimum: 12 px for labels, 14 px for body on mobile, 16 px for reading text.
- **Opacity-based grey text often fails contrast**: 40% white on near-black is about 3.7:1; #6A6862 on #E4E2DE is about 4.3:1. Use solid tokens checked to 4.5:1, not opacity.
- **Preloaders and intros failed** in a headless browser on Hyve, Tenbin and Ventriloc. Never block content.
- **Consent walls** covered the whole mobile viewport on UNITED24, Mode and Perplexity. Keep any consent UI to a small bar.
- **Glass and blend modes** are GPU-heavy on low-end Android, common in all three of our markets; Arrow Dynamics stacks 156 overlay-blend elements and 152 blurred ones.

---

## 7. Screenshot files

Folder: `/tmp/claude-0/-home-user-42/5c5ff21d-b70b-5237-8c1e-a52d1e61acf3/scratchpad/refs/` (session scratchpad, not the shared drive).

Desktop 1440x900 (`<site>.png`) and mobile 390x844 (`<site>-mobile.png`) for: arrowdynamics, askmidnight, earth-nullschool, elvalabs, enpowertrading, flightradar24, hardwareoperations, hyve, lusion, mode, perplexity-personal-computer, radio-garden, riact, rig, submarinecablemap, tenbinlabs, typeform-ai-data-report-2026, united24-rebuild-map, uptec, usevault, ventriloc, voximplant, yauch-oscilloscope.

Scrolled desktop views (`<site>-scrolled.png`) for: askmidnight, elvalabs, enpowertrading, hyve, lusion, mode, riact, rig, tenbinlabs, typeform-ai-data-report-2026, united24-rebuild-map, usevault, ventriloc.

Caveats: hyve, tenbinlabs and ventriloc screenshots show only their preloaders; riact's hero video area shows a third-party "couldn't verify" error; several images include cookie banners; the radio-garden desktop image is the "Press play" splash. Style measurements for all of these were still taken from the loaded DOM.

Not captured (unverified): Smart Web Agency article and every site in it; Pinnacle Design Lab (proxy refused the connection).
