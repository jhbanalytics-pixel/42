/* The 42 rail (EXPERIENCE.md, Navigation), the app's only navigation. The
   vendored shell still renders its five jobs; rail42.css hides them. UX
   pass, 3 October 2026: the rail is named groups. Start here (Today, Ask,
   Discover), Your work (Alerts, Investigations, Dossiers, History), then
   More with Dig deeper and How 42 works. Build left the menu but #/console
   still opens it. The wide rail is the left column and comes first in the
   document, behind its own skip link; below 1024 px it folds into a bottom
   bar of four pages and More. Board, Listen, Network and Browse read the
   desk API, which f42-api does not serve, so their links open the 42 page
   that does the same job (legacyRoutes.js). */
import {useCallback, useEffect, useId, useRef, useState} from 'react';
import {useIsMobile} from './parts.jsx';
import './styles/rail42.css';

const MORE_EXIT_MS = 120;

/* routes[0] is where the hash lands; the rest are the pages under it that
   mark the same link. The menu teaches one path (UX pass, 3 October 2026):
   see what is moving (Start here), keep what matters (Your work), and every
   checking or specialist page under More, in two named groups. */
export const RAIL_TOP = Object.freeze([
  {label: 'Today', href: '#/pulse', routes: ['pulse']},
  {label: 'Ask', href: '#/ask', routes: ['ask']},
  {label: 'Discover', href: '#/explore', routes: ['explore']},
]);

export const RAIL_WORK = Object.freeze([
  {label: 'Alerts', href: '#/alerts', routes: ['alerts']},
  {label: 'Investigations', href: '#/investigations', routes: ['investigations', 'investigation']},
  {label: 'Dossiers', href: '#/dossiers', routes: ['dossiers', 'dossier', 'dossier-share']},
  {label: 'History', href: '#/history', routes: ['history', 'history-item']},
]);

/* Build left the menu: it repeats Ask, Investigations and Dossiers on one
   page. #/console still opens it. */
export const RAIL_MORE_GROUPS = Object.freeze([
  {id: 'deeper', title: 'Dig deeper', items: Object.freeze([
    {label: 'Compare', href: '#/compare', routes: ['compare']},
    {label: 'Lexicon', href: '#/lexicon', routes: ['lexicon']},
    {label: 'Communities', href: '#/communities', routes: ['communities', 'community']},
    {label: 'Seed path', href: '#/seedpath', routes: ['seedpath']},
    {label: 'Seeds', href: '#/seeds', routes: ['seeds']},
  ])},
  {id: 'how', title: 'How 42 works', items: Object.freeze([
    {label: 'Coverage', href: '#/coverage', routes: ['coverage']},
    {label: 'Fieldwork', href: '#/fieldwork', routes: ['fieldwork']},
    {label: 'Method', href: '#/method', routes: ['method']},
    {label: 'Schedules', href: '#/schedules', routes: ['schedules']},
    {label: 'Skins', href: '#/skins', routes: ['skins', 'skin']},
    {label: 'Hidden people', href: '#/people/hidden', routes: ['people-hidden']},
    {label: 'All pages', href: '#/map', routes: ['map']},
  ])},
]);

export const RAIL_MORE = Object.freeze(RAIL_MORE_GROUPS.flatMap((group) => group.items));

/* Every page the menu names, in reading order. */
export const RAIL_ALL = Object.freeze([...RAIL_TOP, ...RAIL_WORK, ...RAIL_MORE]);

/* At phone width the bar holds four pages; the rest of Your work leads the
   More sheet. */
const BAR_ITEMS = Object.freeze([...RAIL_TOP, RAIL_WORK[0]]);
const SHEET_WORK = Object.freeze(RAIL_WORK.slice(1));

const THEMES = Object.freeze([['daylight', 'Light'], ['midnight', 'Dark'], ['system', 'Match system']]);

/* Ask is also the console while it shows its question page. */
function currentHref(route, askOpen){
  if (route === 'console' && askOpen) return '#/ask';
  const item = RAIL_ALL.find((entry) => entry.routes.includes(route));
  return item ? item.href : null;
}

function Links({items, here}){
  return items.map((item) => (
    <li key={item.href}>
      <a className="rail42-link" href={item.href} aria-current={item.href === here ? 'page' : undefined}>{item.label}</a>
    </li>
  ));
}

/* A named group of links: the name is a quiet label, read out as the
   list's name. */
function Group({id, title, items, here, group}){
  return (
    <div className="rail42-group">
      <p className="rail42-group-title" id={id}>{title}</p>
      <ul className="rail42-list" data-rail-group={group} aria-labelledby={id}><Links items={items} here={here} /></ul>
    </div>
  );
}

function ThemeChoice({theme, onThemeChange}){
  const id = useId();
  return (
    <div className="rail42-theme">
      <label htmlFor={id}>Theme</label>
      <select id={id} value={theme} onChange={(event) => onThemeChange(event.target.value)}>
        {THEMES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>
    </div>
  );
}

/* The shell's skip link would sit after every rail link, so the wide rail
   brings its own, first in the document, with the shell's styling. It moves
   focus itself: a bare #instrument-workspace hash is a route change to the
   host router, which would leave the page. */
function SkipLink(){
  const skip = (event) => {
    const workspace = document.getElementById('instrument-workspace');
    if (!workspace) return;
    event.preventDefault();
    workspace.focus();
  };
  return <a className="instrument-skip-link" href="#instrument-workspace" onClick={skip}>Skip to workspace</a>;
}

/* App mounts the rail twice: only="wide" before the shell and only="compact"
   inside it after the workspace, where a dialog the page opens still paints
   over the bar. Each renders only at its own width. */
export function Rail42({route, askOpen = false, theme, onThemeChange, only}){
  const compact = useIsMobile(1023);
  if ((only === 'wide' && compact) || (only === 'compact' && !compact)) return null;
  return <RailAt compact={compact} route={route} askOpen={askOpen} theme={theme} onThemeChange={onThemeChange} />;
}

function RailAt({compact, route, askOpen, theme, onThemeChange}){
  const here = currentHref(route, askOpen);
  const sheetItems = compact ? [...SHEET_WORK, ...RAIL_MORE] : RAIL_MORE;
  const held = sheetItems.find((item) => item.href === here) || null;
  /* At desktop a page under More opens the list, so its marked link is on
     screen. At phone width the sheet would cover the page, so it stays shut
     and the More button names the page instead. */
  const initialOpen = Boolean(held) && !compact;
  const [open, setOpen] = useState(initialOpen);
  const [panelMounted, setPanelMounted] = useState(initialOpen);
  const [motionState, setMotionState] = useState(initialOpen ? 'open' : 'closed');
  const openRef = useRef(initialOpen);
  const closeTimerRef = useRef(null);
  const motionGenerationRef = useRef(0);
  const moreRef = useRef(null);
  const sheetRef = useRef(null);
  const panelId = useId();

  const setPanelOpen = useCallback((next) => {
    const wasOpen = openRef.current;
    const nextOpen = typeof next === 'function' ? next(wasOpen) : next;
    if (nextOpen === wasOpen) return;
    openRef.current = nextOpen;
    const generation = ++motionGenerationRef.current;
    if (closeTimerRef.current !== null){
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    setOpen(nextOpen);
    if (nextOpen){
      setPanelMounted(true);
      setMotionState('open');
      return;
    }
    if (document.documentElement.getAttribute('data-motion') === 'off' || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches){
      setPanelMounted(false);
      setMotionState('closed');
      return;
    }
    setPanelMounted(true);
    setMotionState('closing');
    closeTimerRef.current = window.setTimeout(() => {
      if (motionGenerationRef.current !== generation || openRef.current) return;
      closeTimerRef.current = null;
      setPanelMounted(false);
      setMotionState('closed');
    }, MORE_EXIT_MS);
  }, []);

  useEffect(() => {
    setPanelOpen(Boolean(held) && !compact);
  }, [route, askOpen, compact, held, setPanelOpen]);

  useEffect(() => () => {
    motionGenerationRef.current += 1;
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current);
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    const away = (event) => {
      if (sheetRef.current && sheetRef.current.contains(event.target)) return;
      if (moreRef.current && moreRef.current.contains(event.target)) return;
      setPanelOpen(false);
    };
    document.addEventListener('pointerdown', away);
    return () => document.removeEventListener('pointerdown', away);
  }, [open, setPanelOpen]);

  const onKeyDown = (event) => {
    if (event.key !== 'Escape' || !open) return;
    setPanelOpen(false);
    if (moreRef.current) moreRef.current.focus();
  };

  const more = (
    <div className="rail42-more">
      <button
        ref={moreRef}
        type="button"
        className="rail42-more-button"
        aria-expanded={open}
        aria-controls={panelId}
        data-holds-current={compact && held ? '' : undefined}
        onClick={() => setPanelOpen((value) => !value)}
      >
        <span className="rail42-more-label">More</span>{compact && held ? <span className="sr-only">, you are on {held.label}</span> : null}
        {!compact ? <span className="rail42-more-chevron" aria-hidden="true" /> : null}
      </button>
      <div
        id={panelId}
        ref={sheetRef}
        className="rail42-sheet"
        hidden={!panelMounted}
        aria-hidden={open ? undefined : 'true'}
        inert={open ? undefined : ''}
        data-motion-state={motionState}
      >
        {compact ? <Group id={`${panelId}-work`} title="Your work" items={SHEET_WORK} here={here} /> : null}
        {RAIL_MORE_GROUPS.map((group) => <Group key={group.id} id={`${panelId}-${group.id}`} title={group.title} items={group.items} here={here} />)}
        {compact ? <ThemeChoice theme={theme} onThemeChange={onThemeChange} /> : null}
      </div>
    </div>
  );

  if (compact){
    return (
      <nav className="rail42 rail42--bar" aria-label="Main" data-rail-layout="bar" onKeyDown={onKeyDown}>
        <ul className="rail42-list" data-rail-group="bar"><Links items={BAR_ITEMS} here={here} /></ul>
        {more}
      </nav>
    );
  }
  return (
    <>
      <SkipLink />
      <nav className="rail42" aria-label="Main" data-rail-layout="rail" onKeyDown={onKeyDown}>
        <Group id={`${panelId}-top`} title="Start here" items={RAIL_TOP} here={here} group="top" />
        <Group id={`${panelId}-work`} title="Your work" items={RAIL_WORK} here={here} group="work" />
        {more}
        <div className="rail42-foot"><ThemeChoice theme={theme} onThemeChange={onThemeChange} /></div>
      </nav>
    </>
  );
}
