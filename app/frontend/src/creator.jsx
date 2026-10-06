import {useMemo, useState} from 'react';
import {StateView} from 'ogilvy-intelligence-design-system';
import {useApi} from './api.js';
import {BigChart} from './charts.jsx';
import {human, profileUrl, regionLabel} from './model.js';
import {routeStateView} from './instrumentRouteModels.js';
import './styles/creator.css';

const CRM_KEY = 'pulse-crm';

function loadCrm(){
  try { const value = JSON.parse(localStorage.getItem(CRM_KEY) || '[]'); return Array.isArray(value) ? value : []; }
  catch { return []; }
}

function useCrm(){
  const [saved, setSaved] = useState(loadCrm);
  const [error, setError] = useState('');
  const names = useMemo(() => new Set(saved.filter((item) => item && typeof item.name === 'string').map((item) => normalizeHandle(item.name))), [saved]);
  const toggle = (handle) => {
    const next = names.has(normalizeHandle(handle)) ? saved.filter((item) => normalizeHandle(item?.name) !== normalizeHandle(handle)) : [...saved, {name: handle}];
    try { localStorage.setItem(CRM_KEY, JSON.stringify(next)); setSaved(next); setError(''); }
    catch { setError('This browser could not save your tracked handles.'); }
  };
  return [names, toggle, error];
}

function normalizeHandle(value){
  return typeof value === 'string' ? value.trim().replace(/^@+/, '').toLowerCase() : '';
}

export function creatorSourceUrl(value){
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

function measured(value){
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

export function creatorCount(value){
  return measured(value) ? human(value) : 'Unmeasured';
}

function SourceDate({value, label}){
  const date = typeof value === 'string' ? new Date(value) : null;
  if (!date || !Number.isFinite(date.getTime())) return <span>{label} date unavailable</span>;
  return <span>{label} <time dateTime={value}>{date.toLocaleDateString('en-ZA', {timeZone: 'UTC', day: 'numeric', month: 'short', year: 'numeric'})}</time></span>;
}

export function CreatorProfile({handle, session, onAuth}){
  const [data, retry] = useApi('/api/creator/' + encodeURIComponent(handle), session, onAuth, Boolean(normalizeHandle(handle)));
  const [tracked, toggleTrack, trackError] = useCrm();
  const base = <a className="creator-back" href="#/network">Back to Network</a>;
  const state = (title, body, loading = false) => <div className="page creator-page" data-creator-state={loading ? 'loading' : 'unavailable'}>{base}<StateView {...routeStateView({state: loading ? 'loading' : 'error', title, body, task: loading ? 'Reading observed activity and source posts' : undefined, code: loading ? undefined : data.code, reservedRows: 3, actions: loading ? [] : [{id: 'retry-profile', label: 'Try again', onClick: retry}]})} /></div>;
  if (data.state === 'loading') return state('Reading the creator profile', 'Loading observed activity and source posts.', true);
  if (data.state === 'auth') return state('Authentication is required', 'Sign in before reading this profile.');
  if (data.state !== 'ready') return state('The profile could not load', 'The requested profile is unavailable. This does not establish that the handle has no activity.');
  const c = data.data;
  if (!c || !normalizeHandle(handle) || normalizeHandle(c.handle) !== normalizeHandle(handle) || !Array.isArray(c.wall) || c.wall.some((post) => !post || typeof post !== 'object')) return state('The profile could not be verified', 'The returned profile did not match the requested handle or its post collection.');
  const on = tracked.has(normalizeHandle(c.handle));
  const markets = Array.isArray(c.markets) ? c.markets.map((market) => regionLabel(String(market).toUpperCase())) : [];
  const platforms = Array.isArray(c.platforms) ? c.platforms : [];
  const topics = Array.isArray(c.topics) ? c.topics.filter((item) => typeof item?.id === 'string' && item.id) : [];
  const series = Array.isArray(c.reach_series) ? c.reach_series : [];
  const validSeries = series.length > 1 && series.every((point) => measured(point?.reach) && typeof point.date === 'string' && Number.isFinite(Date.parse(point.date)));
  const accountUrl = creatorSourceUrl(profileUrl(c.handle, c.platform));
  const stats = [
    ['Recorded engagement', c.total_engagement], ['Posts recorded', c.posts],
    ['Average per post', c.avg_engagement], ['Top post engagement', c.top_engagement],
  ];
  return (
    <div className="page creator-page" data-creator-state="ready">
      {base}
      <header className="creator-header">
        <div><p className="creator-eyebrow">Creator profile</p><h1 aria-label={'@' + c.handle} data-identifier={'@' + c.handle}>@{String(c.handle).split(/([._-])/).flatMap((part, index) => /^[._-]$/.test(part) ? [part, <wbr key={index} />] : [part])}</h1><p className="creator-intro">The activity behind the handle.</p></div>
        <div className="creator-actions">
          <button type="button" onClick={() => toggleTrack(c.handle)} aria-pressed={on}>{on ? 'Tracking handle' : 'Track this handle'}</button>
          <a href="#/board">My board</a>
          {accountUrl && <a href={accountUrl} target="_blank" rel="noreferrer">Open account or search</a>}
        </div>
      </header>
      {trackError && <p role="alert">{trackError}</p>}
      <div className="creator-scope">
        <p>{markets.length ? markets.join(' / ') : 'Markets unavailable'} · {platforms.length ? platforms.join(' / ') : 'Platforms unavailable'}</p>
        <p>Records collected in the service's rolling 30-day window. This profile groups the same handle across returned markets and platforms; it does not verify that every record belongs to one account.</p>
      </div>
      <dl className="creator-stats">{stats.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{creatorCount(value)}</dd></div>)}</dl>
      <p className="creator-note">The service caps engagement at 10 million interactions per post and counts missing engagement as zero. Totals, averages and this chart use those values. Unique audience size is unmeasured.</p>
      {validSeries && <section className="creator-series" aria-label="Recorded engagement over time"><BigChart label="Recorded engagement over time" values={series.map((point) => point.reach)} dates={series.map((point) => point.date)} format={human} height={220} color="var(--ink)" /></section>}
      {topics.length > 0 && <section className="creator-topics"><h2>Topics in these records</h2><p>Topic stories open in the market selected in the header.</p><div>{topics.map((topic) => <a key={topic.id} href={'#/topic/' + encodeURIComponent(topic.id)}>{topic.label || topic.id}</a>)}</div></section>}
      <section className="creator-wall" aria-labelledby="creator-posts-title">
        <div className="creator-section-heading"><h2 id="creator-posts-title">Source posts</h2><span>{c.wall.length} returned</span></div>
        <p className="creator-note">The returned selection may be smaller than the full post count. Publication and collection dates are shown separately where supplied.</p>
        {c.wall.length === 0 ? <p>No post selection was returned. The profile totals may cover records outside this selection.</p> : c.wall.map((post, index) => {
          const href = creatorSourceUrl(post.url);
          return <article className="creator-post" key={[post.platform, post.market, post.id, index].join(':')}>
            <header><span>{post.platform || 'Platform unavailable'} · {post.market ? regionLabel(String(post.market).toUpperCase()) : 'Market unavailable'}</span><span>{creatorCount(post.engagement)} engagement</span></header>
            <p className="creator-post-text">{post.text || 'Post text unavailable.'}</p>
            <div className="creator-post-dates"><SourceDate value={post.published_at} label="Published" /><SourceDate value={post.collected_at} label="Collected" /></div>
            {href ? <a href={href} target="_blank" rel="noreferrer">View original post</a> : <p className="creator-note">Source link unavailable.</p>}
          </article>;
        })}
      </section>
    </div>
  );
}
