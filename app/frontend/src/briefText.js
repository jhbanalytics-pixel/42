const MOMENTUM_LABELS = {
  rising: 'Rising',
  building: 'Building',
  steady: 'Steady',
  cooling: 'Cooling',
};

export function hasBriefContent(brief){
  if (!brief || typeof brief !== 'object' || Array.isArray(brief)) return false;
  return [brief.trend, brief.relevance, brief.idea?.text].some(value => typeof value === 'string' && Boolean(value.trim()));
}

export function briefText(t){
  if (!hasBriefContent(t?.brief)) return '';
  const b = t.brief || {};
  const idea = b.idea || {};
  const tool = idea.tool === 'Nano' ? 'Nano Banana' : (idea.tool || 'Nano Banana');
  const L = ['42 BRIEF · ' + t.topic.toUpperCase() + ' · ' + t.regionName + ' (' + t.region + ')'];
  const meta = [MOMENTUM_LABELS[t.momentum]];
  if (typeof t.score === 'number') meta.push('score ' + t.score.toFixed(3));
  if (typeof t.mentions === 'number') meta.push(t.mentions + ' mentions');
  if (typeof t.creators === 'number') meta.push(t.creators + ' creators');
  if (typeof t.sources === 'number') meta.push(t.sources + ' sources');
  L.push(meta.join(' · '));
  if (t.voices && t.voices.length){
    L.push('', 'ON THE GROUND · CITED VOICES');
    t.voices.forEach((g) => L.push('· [' + g[0] + (g[2] ? ' · ' + g[2] : '') + '] “' + g[1] + '”'));
  }
  L.push('');
  if (b.trend) L.push('1. THE TREND: ' + b.trend);
  if (b.relevance) L.push('2. THE RELEVANCE: ' + b.relevance);
  if (b.opportunity) L.push('3. THE OPPORTUNITY: ' + b.opportunity);
  if (idea.text) L.push('4. THE IDEA (' + tool + '): ' + idea.text);
  if (b.prompt && (b.prompt.nano || b.prompt.lyria)){
    L.push('5. THE PROMPT');
    if (b.prompt.nano) L.push('   · Nano Banana / image: ' + b.prompt.nano);
    if (b.prompt.lyria) L.push('   · Lyria / music: ' + b.prompt.lyria);
  }
  L.push('', 'North star: turn this signal into participation with Nano Banana and Lyria · 42 · Ogilvy Intelligence');
  return L.join('\n');
}
