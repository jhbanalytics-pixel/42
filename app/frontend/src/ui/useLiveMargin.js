import {useCallback, useEffect, useRef, useState} from 'react';

import {LIVE_MARGIN_CHAPTERS, chapterProgress, scrollBehavior} from '../liveMarginContract.js';

const NOOP = () => {};

function isLiveMarginChapter(chapterId){
  return LIVE_MARGIN_CHAPTERS.includes(chapterId);
}

function prefersReducedMotion(){
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function readingEdgeDistance(entry){
  const edge = Number.isFinite(entry.rootBounds?.top) ? entry.rootBounds.top : 0;
  const top = entry.boundingClientRect?.top;
  return Number.isFinite(top) ? Math.abs(top - edge) : Number.POSITIVE_INFINITY;
}

export function observeLiveMarginChapters(chapterTargets, onActiveChapter){
  if (typeof window === 'undefined' || typeof window.IntersectionObserver !== 'function') return NOOP;
  const browserWindow = window;

  const targets = [...chapterTargets].filter(([chapterId, target]) => (
    isLiveMarginChapter(chapterId) && target
  ));
  if (!targets.length) return NOOP;

  let active = true;
  let scrollFrame = null;
  const chapterByTarget = new Map(targets.map(([chapterId, target]) => [target, chapterId]));
  const visibleByTarget = new Map();
  const currentScrollChapter = () => {
    const viewportHeight = browserWindow.innerHeight;
    if (!Number.isFinite(viewportHeight)) return null;
    const documentElement = browserWindow.document?.documentElement;
    const scrollHeight = documentElement?.scrollHeight;
    const scrollY = Number.isFinite(browserWindow.scrollY) ? browserWindow.scrollY : documentElement?.scrollTop;
    const response = targets.find(([chapterId]) => chapterId === 'response');
    if (
      response
      && Number.isFinite(scrollHeight)
      && Number.isFinite(scrollY)
      && scrollY + viewportHeight >= scrollHeight - 1
    ) return response[0];

    const readingLine = viewportHeight * 0.6;
    let selectedChapter = LIVE_MARGIN_CHAPTERS[0];
    let selectedIndex = -1;
    for (const [chapterId, target] of targets){
      if (typeof target.getBoundingClientRect !== 'function') continue;
      const top = target.getBoundingClientRect().top;
      const chapterIndex = LIVE_MARGIN_CHAPTERS.indexOf(chapterId);
      if (Number.isFinite(top) && top <= readingLine && chapterIndex > selectedIndex){
        selectedChapter = chapterId;
        selectedIndex = chapterIndex;
      }
    }
    return selectedChapter;
  };
  const evaluateScroll = () => {
    scrollFrame = null;
    if (!active) return;
    const chapterId = currentScrollChapter();
    if (chapterId) onActiveChapter(chapterId);
  };
  const canEvaluateScroll = typeof browserWindow.addEventListener === 'function'
    && typeof browserWindow.requestAnimationFrame === 'function';
  const scheduleScrollEvaluation = () => {
    if (!active || scrollFrame !== null) return;
    scrollFrame = browserWindow.requestAnimationFrame(evaluateScroll);
  };
  const onScroll = () => scheduleScrollEvaluation();
  const observer = new browserWindow.IntersectionObserver((entries) => {
    if (!active) return;
    for (const entry of entries){
      if (!chapterByTarget.has(entry.target)) continue;
      if (entry.isIntersecting) visibleByTarget.set(entry.target, entry);
      else visibleByTarget.delete(entry.target);
    }
    if (canEvaluateScroll){
      scheduleScrollEvaluation();
      return;
    }
    const nearest = [...visibleByTarget.values()].reduce((current, candidate) => {
      if (!current) return candidate;
      const distance = readingEdgeDistance(candidate) - readingEdgeDistance(current);
      if (distance < 0) return candidate;
      if (distance > 0) return current;
      const candidateId = chapterByTarget.get(candidate.target);
      const currentId = chapterByTarget.get(current.target);
      return LIVE_MARGIN_CHAPTERS.indexOf(candidateId) < LIVE_MARGIN_CHAPTERS.indexOf(currentId)
        ? candidate
        : current;
    }, null);
    if (nearest) onActiveChapter(chapterByTarget.get(nearest.target));
  });
  targets.forEach(([, target]) => observer.observe(target));
  if (canEvaluateScroll) browserWindow.addEventListener('scroll', onScroll, {passive: true});

  return () => {
    active = false;
    if (typeof browserWindow.removeEventListener === 'function') browserWindow.removeEventListener('scroll', onScroll);
    if (scrollFrame !== null && typeof browserWindow.cancelAnimationFrame === 'function'){
      browserWindow.cancelAnimationFrame(scrollFrame);
      scrollFrame = null;
    }
    visibleByTarget.clear();
    observer.disconnect();
  };
}

export function scrollToLiveMarginChapter(chapterId, target, reduced){
  if (!isLiveMarginChapter(chapterId) || !target || typeof target.scrollIntoView !== 'function') return false;
  target.scrollIntoView({behavior: scrollBehavior(reduced), block: 'start'});
  return true;
}

export function useLiveMargin(){
  const [activeChapter, setActiveChapter] = useState(LIVE_MARGIN_CHAPTERS[0]);
  const [chapterTargets, setChapterTargets] = useState(() => new Map());
  const chapterRefCallbacks = useRef(new Map());

  const registerChapter = useCallback((chapterId) => {
    if (!isLiveMarginChapter(chapterId)) return NOOP;
    const existing = chapterRefCallbacks.current.get(chapterId);
    if (existing) return existing;

    const register = (target) => {
      setChapterTargets((current) => {
        if (current.get(chapterId) === target) return current;
        const next = new Map(current);
        if (target) next.set(chapterId, target);
        else next.delete(chapterId);
        return next;
      });
    };
    chapterRefCallbacks.current.set(chapterId, register);
    return register;
  }, []);

  useEffect(() => observeLiveMarginChapters(chapterTargets, setActiveChapter), [chapterTargets]);

  const navigateToChapter = useCallback((chapterId) => {
    const didNavigate = scrollToLiveMarginChapter(
      chapterId,
      chapterTargets.get(chapterId),
      prefersReducedMotion(),
    );
    if (didNavigate) setActiveChapter(chapterId);
    return didNavigate;
  }, [chapterTargets]);

  return {
    activeChapter,
    progress: chapterProgress(activeChapter),
    registerChapter,
    navigateToChapter,
  };
}
