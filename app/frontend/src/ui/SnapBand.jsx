import {useCallback, useEffect, useRef, useState} from 'react';
import '../styles/today-boards.css';

/* The named scroll box (c42-scroll is the geometry gate's name for one) of every band and strip. The band: every card of one market in a single row that scrolls sideways.
   The row snaps card by card, moves by swipe, trackpad or shift and wheel,
   by the left and right keys while it has focus, and by the two edge arrows.
   An arrow is only there while more cards are off that edge, and the soft
   fade on that edge goes with it. The vertical wheel is left to the page, so
   a reader scrolling down is never caught by the row. */
const EDGE = 2;
const still = () => typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function Chevron({back}){
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false" className="tb-arrow-mark">
      <path d={back ? 'M15 5l-7 7 7 7' : 'M9 5l7 7-7 7'} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="square" />
    </svg>
  );
}

export function SnapBand({label, children, prevLabel, nextLabel, itemSelector, before = null, className = ''}){
  const scroller = useRef(null);
  const [edges, setEdges] = useState({before: false, after: false});
  const measure = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    const left = el.scrollLeft;
    const before = left > EDGE;
    const after = el.scrollWidth - el.clientWidth - left > EDGE;
    /* A focused arrow that is about to leave hands its focus to the row, so a
       keyboard reader is never dropped onto the page. */
    const held = document.activeElement;
    if (held && held.closest && ((!after && held.closest('[data-band-arrow="next"]')) || (!before && held.closest('[data-band-arrow="prev"]')))){
      el.focus({preventScroll: true});
    }
    setEdges((now) => (now.before === before && now.after === after ? now : {before, after}));
  }, []);
  useEffect(() => {
    measure();
    const el = scroller.current;
    const watch = typeof ResizeObserver === 'function' && el ? new ResizeObserver(measure) : null;
    if (watch){
      watch.observe(el);
      if (el.firstElementChild) watch.observe(el.firstElementChild);
    }
    window.addEventListener('resize', measure);
    return () => {
      if (watch) watch.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, [measure]);
  const move = (sign, amount) => {
    const el = scroller.current;
    if (el) el.scrollBy({left: sign * amount, behavior: still() ? 'auto' : 'smooth'});
  };
  /* An arrow moves most of a view; a key moves one card and its gap. */
  const view = () => Math.max(1, Math.round(scroller.current.clientWidth * 0.85));
  const step = () => {
    const el = scroller.current;
    const first = el.querySelector(itemSelector);
    const width = first ? first.getBoundingClientRect().width : 0;
    if (!(width > 0)) return Math.max(1, Math.round(el.clientWidth / 2));
    const track = el.firstElementChild;
    const gap = track ? parseFloat(window.getComputedStyle(track).columnGap) : 0;
    return Math.round(width + (Number.isFinite(gap) ? gap : 0));
  };
  const onKeyDown = (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.key === 'ArrowRight' && edges.after){
      event.preventDefault();
      move(1, step());
    } else if (event.key === 'ArrowLeft' && edges.before){
      event.preventDefault();
      move(-1, step());
    }
  };
  return (
    <div className={className ? 'tb-band ' + className : 'tb-band'} data-band="" data-scrollable={edges.before || edges.after ? '' : undefined} data-more-before={edges.before ? '' : undefined} data-more-after={edges.after ? '' : undefined}>
      {before}
      {(edges.before || edges.after) && (
        <div className="tb-arrows">
          {edges.before && (
            <button type="button" className="tb-arrow tb-arrow-prev" data-band-arrow="prev" aria-label={prevLabel} onClick={() => move(-1, view())}>
              <Chevron back />
            </button>
          )}
          {edges.after && (
            <button type="button" className="tb-arrow tb-arrow-next" data-band-arrow="next" aria-label={nextLabel} onClick={() => move(1, view())}>
              <Chevron />
            </button>
          )}
        </div>
      )}
      <div className="tb-scroll c42-scroll" data-band-scroll="" ref={scroller} role="region" aria-label={label} tabIndex={0} onScroll={measure} onKeyDown={onKeyDown}>
        <div className="tb-track" data-band-track="">{children}</div>
      </div>
    </div>
  );
}
