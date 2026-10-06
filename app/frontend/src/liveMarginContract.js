export const LIVE_MARGIN_CHAPTERS = ['why-now', 'proof', 'precedent', 'response'];

export function chapterProgress(activeId){
  const index = LIVE_MARGIN_CHAPTERS.indexOf(activeId);
  if (index === -1) throw new RangeError('Unknown Live Margin chapter');
  return (index + 1) / LIVE_MARGIN_CHAPTERS.length;
}

export function scrollBehavior(reduced){
  return reduced ? 'auto' : 'smooth';
}
