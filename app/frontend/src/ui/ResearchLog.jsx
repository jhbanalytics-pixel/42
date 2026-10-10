/* 42 Ask · ResearchLog: the agent's steps in plain words with their counts.
   Live while the question runs it is an instrument panel: the step in hand as
   one plain line, what has been read so far as counts, and every earlier step
   folded into a quiet log beside it. Afterwards it folds into "How this was
   researched · N steps". The step list is a polite live region either way.

   Step lines are written by the research tools and can carry the archive's
   own table and column names or the depth code. The reader sees what is being
   done, not how the archive is laid out, so those lines are said in plain
   words here and a run of the same line reads once with its count. */
import {useEffect, useState} from 'react';
import {platformLabel} from './PlatformGlyph.jsx';

/* A search query's terms as a reader would say them: "sound OR challenge OR
   trend" reads "sounds, challenges and trends". Only a plain lower-case word
   takes a plural; a hashtag, a name or a phrase is kept as written. */
function plural(term){
  if (!/^[a-z]{3,}$/.test(term) || /s$/.test(term)) return term;
  if (/[^aeiou]y$/.test(term)) return term.slice(0, -1) + 'ies';
  if (/(?:ch|sh|x|z)$/.test(term)) return term + 'es';
  return term + 's';
}
function listWords(items){
  if (items.length < 2) return items.join('');
  return items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1];
}
function queryWords(query){
  const terms = String(query || '')
    .replace(/["()]/g, ' ')
    .split(/\s+(?:OR|AND|or|and)\s+|\s*[,|]\s*/)
    .map((term) => term.trim())
    .filter(Boolean);
  const seen = new Set();
  const unique = terms.filter((term) => !seen.has(term.toLowerCase()) && seen.add(term.toLowerCase()));
  return listWords(unique.length > 1 ? unique.map(plural) : unique);
}

/* "Searching stored posts for 'q' on TikTok, South Africa, 28 September to
   4 October" reads "Searching stored TikTok posts about q in South Africa,
   28 September to 4 October". The platforms, the market and the dates are
   each optional; the dates are the part that carries a number. */
function searchWords(query, rest){
  const on = /^\s+on\s+/.test(rest);
  const body = rest.replace(/^\s+on\s+|^\s*,\s*/, '').trim();
  const parts = body ? body.split(/\s*,\s*/) : [];
  const dates = parts.length && /\d/.test(parts[parts.length - 1]) ? parts.pop() : null;
  const market = dates && parts.length > (on ? 1 : 0) ? parts.pop() : (!on && parts.length ? parts.pop() : null);
  const platforms = on ? parts : [];
  const terms = queryWords(query);
  return 'Searching stored ' + (platforms.length ? listWords(platforms) + ' ' : '') + 'posts'
    + (terms ? ' about ' + terms : '')
    + (market ? ' in ' + market : '')
    + (dates ? ', ' + dates : '');
}

/* A step line in reader words. The text is only reworded where it names the
   archive's inner workings or carries a raw search query; every other line is
   kept as the agent wrote it. */
export function stepWords(text){
  let words = String(text || '').trim();
  if (!words) return 'Working on the question';
  if (/^Counting in the warehouse\b/i.test(words)) return 'Counting posts in the archive';
  if (/^Checking saved findings\b/i.test(words)) return 'Checking findings already saved on this question';
  const search = /^Searching stored posts for '(.*)'(.*)$/i.exec(words);
  if (search) return searchWords(search[1], search[2]);
  if (/^A warehouse count\b/i.test(words)) return words.replace(/^A warehouse count/i, 'A count in the archive');
  if (/^Using\s+\S+$/.test(words)) return 'Running a lookup';
  words = words.replace(/^Critic:\s*/, 'Reviewing the claims: ');
  words = words.replace(/,?\s*tier T\d\b/g, '');
  words = words.replace(/\s+OR\s+/g, ' or ').replace(/\s+AND\s+/g, ' and ');
  return words;
}

/* Consecutive steps that read the same become one row with a count. */
export function collapseSteps(steps){
  const rows = [];
  (Array.isArray(steps) ? steps : []).forEach((step, index) => {
    const text = stepWords(step && step.text);
    const key = step && step.seq != null ? step.seq : index;
    const last = rows[rows.length - 1];
    if (last && last.text === text){ last.times += 1; return; }
    rows.push({key, kind: step && step.kind, text, times: 1});
  });
  return rows;
}

/* What the research has read so far, from the steps and the posts gathered. */
function tally(steps, evidence, claims, market){
  let posts = 0;
  const platforms = new Set();
  for (const step of steps){
    if (!step) continue;
    if ((step.kind === 'found' || step.kind === 'search') && typeof step.count === 'number' && step.count > 0) posts += step.count;
    /* For a single-market question a platform is read once a step found posts on it. */
    if (step.platform && (!market || (typeof step.count === 'number' && step.count > 0))) platforms.add(platformLabel(step.platform) || String(step.platform));
  }
  for (const record of evidence){
    if (record && record.platform) platforms.add(platformLabel(record.platform) || String(record.platform));
  }
  const checked = claims.filter((item) => item && item.check && item.check !== 'checking').length;
  /* The last step is the one in hand, so it is not yet done. */
  return {steps: steps.length, done: Math.max(steps.length - 1, 0), posts: Math.max(posts, evidence.length), platforms: platforms.size, claims: claims.length, checked};
}

/* A post located in another market is not counted for a single-market question
   (core/agent/ask.py _posts_read). A post with no market, or a question with no
   single market, counts. */
export function inMarket(record, market){
  return !market || !record || !record.market || record.market === market;
}

const clock = (seconds) => Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');

function Elapsed(){
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const started = Date.now();
    const timer = setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, []);
  return <span className="ask42-scan-clock" aria-hidden="true">{clock(seconds)}</span>;
}

function StepRows({rows}){
  return rows.map((row) => (
    <li key={row.key} data-kind={row.kind}>
      {row.text}
      {row.times > 1 && <span className="ask42-log-times">{' ×' + row.times}</span>}
    </li>
  ));
}

export function ResearchLog({steps, running, evidence, claims, action, market, clock: showClock = true, children}){
  const list = Array.isArray(steps) ? steps : [];
  const rows = collapseSteps(list);
  if (running){
    const found = (Array.isArray(evidence) ? evidence : []).filter((record) => inMarket(record, market));
    const checking = Array.isArray(claims) ? claims : [];
    const counts = tally(list, found, checking, market);
    const current = rows[rows.length - 1];
    const earlier = rows.slice(0, -1);
    return (
      <section className="ask42-log ask42-scan" aria-labelledby="ask42-log-title" data-research-live="">
        <div className="ask42-scan-main">
          <div className="ask42-scan-panel">
            <span className="ask42-scan-field" aria-hidden="true"><span className="ask42-scan-sweep" /></span>
            <div className="ask42-scan-head">
              <span className="ask42-scan-beacon" aria-hidden="true" />
              <h3 className="ask42-scan-title" id="ask42-log-title">Researching</h3>
              {showClock && <Elapsed />}
              {action && <div className="ask42-scan-actions">{action}</div>}
            </div>
            {/* Which step is under way, with a row of cells lighting in turn
                beside it while 42 works on it. The cells mark activity, not
                progress, so they carry no scale and the words say the step. */}
            <p className="ask42-scan-step">
              <span className="ask42-scan-step-label">{'Now on step ' + Math.max(list.length, 1)}</span>
              <span className="ask42-scan-cells" aria-hidden="true">
                {Array.from({length: 10}, (_, cell) => <span key={cell} style={{'--cell': cell}} />)}
              </span>
            </p>
            <p className="ask42-scan-now" key={current ? current.key + ':' + current.times : 'start'}>
              {current ? current.text : 'Reading the question'}
              {current && current.times > 1 && <span className="ask42-log-times">{' ×' + current.times}</span>}
            </p>
            <dl className="ask42-scan-counts">
              <div><dt>Steps done</dt><dd>{counts.done}</dd></div>
              <div><dt>Posts found</dt><dd>{counts.posts}</dd></div>
              <div><dt>Platforms read</dt><dd>{counts.platforms}</dd></div>
              {counts.claims > 0 && <div><dt>Claims checked</dt><dd>{counts.checked + ' of ' + counts.claims}</dd></div>}
            </dl>
          </div>
          {children}
        </div>
        <div className="ask42-scan-log">
          <h4 className="ask42-scan-log-title">{earlier.length ? 'Earlier steps' : 'Earlier steps gather here'}</h4>
          <ol className="ask42-log-steps" aria-live="polite">
            <StepRows rows={earlier} />
          </ol>
        </div>
      </section>
    );
  }
  if (!list.length) return null;
  return (
    <details className="ask42-log">
      <summary>{'How this was researched · ' + list.length + (list.length === 1 ? ' step' : ' steps')}</summary>
      <ol className="ask42-log-steps" aria-live="polite">
        <StepRows rows={rows} />
      </ol>
    </details>
  );
}
