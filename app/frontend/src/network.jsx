/* Network pairs latest-brief handle references with returned voice aggregates.
   Record market membership is required; account identity is not established. */
import {useLayoutEffect, useMemo, useRef, useState} from 'react';
import {StateView} from 'ogilvy-intelligence-design-system';
import {FAILURE_ORIGIN_LABEL, failureOrigin, useApi} from './api.js';
import {buildTopicHash} from './router.js';
import {EmptyState, MOM_META} from './parts.jsx';
import {human, regionLabel} from './model.js';
import {networkEmptyFacts, routeRunFacts, routeStateView, tryAgainAction} from './instrumentRouteModels.js';
import './styles/graphs.css';
import './styles/workspaces.css';

/* The two reads land through the package loading frame with a static
   reserved band, the same frame every other route state shows, so nothing
   pulses for the length of the request. */
const NETWORK_WAIT = Object.freeze({
  state: 'loading',
  title: 'Preparing the network',
  task: 'Reading the topics and the voices carrying them',
  body: 'The web draws once both reads for this market have landed.',
  reservedRows: 3,
});

/* one sensible fallback target market, different from whatever is showing
   now: NG unless we are already on NG, then ZA. Simple rotation, not
   exhaustive, this is just an empty-state nudge not a market picker. */
function fallbackMarket(current){
  return String(current || '').toUpperCase() === 'NG' ? 'ZA' : 'NG';
}

const MOM_COLOR = {
  rising: 'var(--rising)',
  building: 'var(--building)',
  steady: 'var(--steady)',
  cooling: 'var(--cooling)',
};

function momColor(m){
  return typeof m === 'string' && Object.hasOwn(MOM_COLOR, m) ? MOM_COLOR[m] : 'var(--accent)';
}

/* strip a leading @ and lowercase, so a topic's '@emily.112056' matches a
   voices handle of 'emily.112056' */
function normHandle(h){
  return typeof h === 'string' ? h.trim().replace(/^@+/, '').toLowerCase() : '';
}

function recordMarket(value){
  const market = typeof value === 'string' ? value.trim().toUpperCase() : '';
  return ['ZA', 'NG', 'KE'].includes(market) ? market : null;
}
const measuredNumber = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const identityKey = (market, name) => JSON.stringify([market, name]);
const rankRecords = field => (a, b) => (b[field] ?? -Infinity) - (a[field] ?? -Infinity) || (a.graphKey < b.graphKey ? -1 : a.graphKey > b.graphKey ? 1 : 0);

/* even vertical spread between lo% and hi% for n nodes (single node centres) */
function spread(i, n, lo, hi){
  if (n <= 1) return (lo + hi) / 2;
  return lo + (i / (n - 1)) * (hi - lo);
}

/* one cubic bezier from the topic node (left) to the creator node (right),
   in the 0..100 viewBox the overlay nodes are positioned against. Quiet
   register, 23 Sept 2026: the curve runs from the inner edge of the topic
   label to the inner edge of the voice label, so the labels need no filled
   box to hide the line under them. */
function edgePath(y1, y2){
  const x1 = 39.5, x2 = 60.5;
  const cx = (x1 + x2) / 2;
  return 'M ' + x1 + ' ' + y1.toFixed(2) +
    ' C ' + cx.toFixed(2) + ' ' + y1.toFixed(2) +
    ' ' + cx.toFixed(2) + ' ' + y2.toFixed(2) +
    ' ' + x2 + ' ' + y2.toFixed(2);
}

function creatorHref(handle){
  return '#/creator/' + encodeURIComponent(normHandle(handle));
}

/* Plain words for the figures the records carry. Nothing here is estimated:
   an engagement the producer did not measure says so. Demo polish,
   2 October 2026: each voice line says "182k engagement"; the note above the
   plot says once that voices are ranked by capped engagement over a rolling
   30-day window, so the cap is not repeated on every line. */
const plural = (n, word) => n + ' ' + word + (n === 1 ? '' : 's');
export function engagementWords(reach){
  return reach === null ? 'Engagement not measured' : human(reach) + ' engagement';
}
function topicWords(topics){
  const names = topics.map((t) => t.topic || t.label);
  if (names.length < 2) return names.join('');
  return names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1];
}

/* The finding the plot states, counted from the drawn records only. Demo
   polish, 2 October 2026: one plain sentence whose verb agrees with its
   count, rather than "1 of 5 voices links two or more topics". */
export function networkFinding(creators, bridges){
  if (!creators) return '';
  if (!bridges) return creators === 1 ? 'The one voice here is linked to one topic only' : 'Each of the ' + creators + ' voices here is linked to one topic only';
  return plural(bridges, 'voice') + (bridges === 1 ? ' connects' : ' connect') + ' two or more topics';
}

/* What the plot shows, said once. Demo polish, 2 October 2026: the matching
   totals are named only when the display holds fewer than matched, rather
   than "4 topic records and 5 voice records from 4 matching topics and 5
   matching voices". */
export function coverageWords(coverage){
  const part = (shown, matched, word) => (shown === matched ? plural(shown, word) : shown + ' of ' + matched + ' matching ' + word + 's');
  return 'Showing ' + part(coverage.displayedTopics, coverage.matchedTopics, 'topic') + ' and ' + part(coverage.displayedCreators, coverage.matchedCreators, 'voice') + '.';
}

export function buildGraph(deskData, voicesData, requestedMarket = 'ALL'){
  const topicsValid = Array.isArray(deskData?.topics), creatorsValid = Array.isArray(voicesData?.creators);
  const requested = typeof requestedMarket === 'string' ? requestedMarket.trim().toUpperCase() : '';
  const scopeValid = requested === 'ALL' || Boolean(recordMarket(requested));
  const responseMarkets = [...(topicsValid ? deskData.topics.map(row => row?.region) : []), ...(creatorsValid ? voicesData.creators.map(row => row?.market) : []), ...(Array.isArray(deskData?.bridges) ? deskData.bridges.map(row => row?.mk) : [])].map(recordMarket).filter(Boolean);
  const scopeMismatch = scopeValid && requested !== 'ALL' && responseMarkets.some(market => market !== requested);
  if (!topicsValid || !creatorsValid || !scopeValid || scopeMismatch) return {validInput: false, reason: scopeMismatch ? 'scope_mismatch' : 'schema_problem', topics: [], creators: [], edges: [], topicRow: new Map(), creatorRow: new Map(), creatorTopics: new Map(), bridge: new Map(), plotHeight: 360, coverage: {receivedTopics: topicsValid ? deskData.topics.length : null, receivedCreators: creatorsValid ? voicesData.creators.length : null, matchedTopics: null, matchedCreators: null}};
  const rawTopics = deskData.topics;
  const rawCreators = voicesData.creators;

  const coverage = {receivedTopics: rawTopics.length, receivedCreators: rawCreators.length, invalidTopicRows: 0, invalidCreatorRows: 0, ambiguousTopics: 0, ambiguousCreators: 0};
  const topicGroups = new Map(), creatorGroups = new Map();
  const group = (groups, row) => { if (!groups.has(row.graphKey)) groups.set(row.graphKey, []); groups.get(row.graphKey).push(row); };
  for (const row of rawTopics){
    const market = recordMarket(row?.region);
    const id = typeof row?.id === 'string' ? row.id.trim() : '';
    if (!market || !id){ coverage.invalidTopicRows++; continue; }
    const label = [row.topic, row.label, id].find(value => typeof value === 'string' && value.trim()).trim();
    group(topicGroups, {id, region: market, graphKey: identityKey(market, id), topic: label, score: measuredNumber(row.score), momentum: typeof row.momentum === 'string' ? row.momentum : null, creators_list: [...new Set((Array.isArray(row.creators_list) ? row.creators_list : []).map(normHandle).filter(Boolean))].sort()});
  }
  for (const row of rawCreators){
    const market = recordMarket(row?.market), handle = normHandle(row?.handle);
    if (!market || !handle){ coverage.invalidCreatorRows++; continue; }
    const platform = typeof row.platform === 'string' ? row.platform.trim().toLowerCase() : '';
    group(creatorGroups, {handle, market, graphKey: identityKey(market, handle), platform, reach: measuredNumber(row.reach), posts: measuredNumber(row.posts)});
  }
  const admittedTopics = [...topicGroups.values()].filter(rows => {
    if (new Set(rows.map(row => JSON.stringify(row))).size === 1) return true;
    coverage.ambiguousTopics++; return false;
  }).map(rows => rows[0]);
  const admittedCreators = [...creatorGroups.values()].filter(rows => {
    if (rows.every(row => row.platform && row.platform !== 'social') && new Set(rows.map(row => JSON.stringify(row))).size === 1) return true;
    coverage.ambiguousCreators++; return false;
  }).map(rows => rows[0]);
  coverage.admittedTopics = admittedTopics.length; coverage.admittedCreators = admittedCreators.length;
  const byIdentity = new Map(admittedCreators.map(row => [row.graphKey, row]));
  const topicLinks = new Map(admittedTopics.map(row => [row.graphKey, row.creators_list.map(handle => identityKey(row.region, handle)).filter(key => byIdentity.has(key))]));
  const connectedTopics = admittedTopics.filter(row => topicLinks.get(row.graphKey).length).sort(rankRecords('score'));
  const allMatched = new Set(connectedTopics.flatMap(row => topicLinks.get(row.graphKey)));
  coverage.matchedTopics = connectedTopics.length; coverage.matchedCreators = allMatched.size;
  const selectedTopics = connectedTopics.slice(0, 7);
  const selectedLinks = new Set(selectedTopics.flatMap(row => topicLinks.get(row.graphKey)));
  const creators = admittedCreators.filter(row => selectedLinks.has(row.graphKey)).sort(rankRecords('reach')).slice(0, 12);
  const displayedCreators = new Set(creators.map(row => row.graphKey));
  const topics = selectedTopics.filter(row => topicLinks.get(row.graphKey).some(key => displayedCreators.has(key)));
  const creatorTopics = new Map(creators.map(row => [row.graphKey, topics.filter(topic => topicLinks.get(topic.graphKey).includes(row.graphKey))]));
  coverage.displayedTopics = topics.length; coverage.displayedCreators = creators.length;
  coverage.limited = topics.length < connectedTopics.length || creators.length < allMatched.size;

  const topicRow = new Map();
  topics.forEach((t, i) => { topicRow.set(t.graphKey, spread(i, topics.length, 10, 90)); });
  const creatorRow = new Map();
  creators.forEach((c, i) => { creatorRow.set(c.graphKey, spread(i, creators.length, 8, 92)); });

  /* a creator is a bridge when it links two or more shown topics */
  const bridge = new Map();
  creators.forEach((c) => {
    const h = c.graphKey;
    bridge.set(h, (creatorTopics.get(h) || []).length >= 2);
  });

  /* one edge per topic-creator link, carrying the topic's momentum colour */
  const edges = [];
  topics.forEach((t) => {
    const y1 = topicRow.get(t.graphKey);
    (topicLinks.get(t.graphKey) || []).forEach((h) => {
      if (!creatorRow.has(h)) return;
      const on = !!bridge.get(h);
      edges.push({
        key: JSON.stringify([t.graphKey, h]),
        topicKey: t.graphKey,
        creatorKey: h,
        y1,
        y2: creatorRow.get(h),
        color: momColor(t.momentum),
        bridge: on,
        opacity: on ? 0.75 : 0.32,
      });
    });
  });

  const plotHeight = Math.max(360, Math.ceil((Math.max(topics.length, creators.length) - 1) * 64 / 0.84));
  return {validInput: true, topics, creators, edges, topicRow, creatorRow, creatorTopics, bridge, plotHeight, coverage};
}

/* Readiness, and where the freshness stamp gets to decide it.

   The desk answer carries the run's stamp and its status on every payload,
   the empty one included, and the graph is drawn from that same answer, so
   the graph dates itself from it rather than claiming the producer supplies
   no age. The stamp only decides a read that has nothing to draw, which is
   where the three desk routes read it too: buildBriefingState reaches its
   stale branch for a run that carries no topics, buildExploreState for a run
   that admitted no signals, and Compare for a run whose comparison holds
   none. A graph that has links is the run's own answer and is not withheld
   because the ingest pipeline behind it has gone amber; the shell already
   shows that stamp in the utility strip.

   An amber status with no stamp cannot be dated, so it is not called stale,
   but it is not a measured empty either: the desk routes take the same run
   to their stale branch, and the route frame shows an undated hold as
   unavailable with checked_at_missing. That run is neither ready nor stale;
   stale is only the dated part of the hold. A stamp is only ever read off an
   amber status, so a dated run is amber unless the caller says otherwise. */
export function networkReadiness({read, validInput, edges, dated, amber = dated}){
  const drawable = Boolean(read && validInput && edges > 0);
  const held = Boolean(read && validInput && !drawable && amber);
  return {drawable, stale: Boolean(held && dated), ready: Boolean(read && validInput && !held)};
}

export function NetworkGraph({region, setRegion, session, onAuth}){
  const reg = String(region || 'ALL');
  const [desk, retryDesk] = useApi('/api/desk?region=' + reg.toLowerCase(), session, onAuth, undefined, 30000);
  const [voices, retryVoices] = useApi('/api/voices?region=' + reg.toLowerCase(), session, onAuth, undefined, 30000);

  const deskData = desk.state === 'ready' && desk.data ? desk.data : {};
  const voicesData = voices.state === 'ready' && voices.data ? voices.data : {};
  const marketLabel = regionLabel(reg);

  const g = useMemo(() => buildGraph(deskData, voicesData, reg), [deskData, voicesData, reg]);
  const plot = useRef(null);
  const [rowPitch, setRowPitch] = useState(64);
  const [hoveredNode, setHoveredNode] = useState(null);
  const [focusedNode, setFocusedNode] = useState(null);
  const activeNode = hoveredNode || focusedNode;
  useLayoutEffect(() => {
    if (!plot.current) return;
    const nodes = [...plot.current.querySelectorAll('.net-node')];
    const measure = () => {
      const tallest = Math.max(48, ...nodes.map((node) => node.getBoundingClientRect().height));
      setRowPitch(Math.ceil(tallest) + 16);
    };
    measure();
    const observer = new ResizeObserver(measure);
    nodes.forEach((node) => observer.observe(node));
    return () => observer.disconnect();
  }, [g]);

  const loading = desk.state === 'loading' || voices.state === 'loading';
  const invalid = desk.state === 'ready' && voices.state === 'ready' && !g.validInput;
  const errored = desk.state === 'error' || voices.state === 'error' || invalid;
  const freshness = desk.state === 'ready' && deskData && typeof deskData.freshness === 'object' ? deskData.freshness : null;
  const runFacts = routeRunFacts({freshness});
  const amber = Boolean(freshness && freshness.status === 'amber');
  const read = desk.state === 'ready' && voices.state === 'ready';
  const routeFailure = [desk, voices].find((result) => result.state === 'error' && result.code === 'http_404' && result.message === 'No such API route.');
  const timeoutFailure = [desk, voices].find((result) => result.state === 'error' && result.code === 'request_timeout');
  const unavailable = Boolean(routeFailure);
  const timedOut = !unavailable && Boolean(timeoutFailure);
  const {stale, ready} = networkReadiness({
    read,
    validInput: g.validInput,
    edges: g.edges.length,
    dated: Boolean(amber && runFacts.checkedAt),
    amber,
  });
  /* A verified read that is not ready is held, dated or not; the route frame
     words a dated hold as stale and an undated one as unavailable. */
  const held = read && g.validInput && !ready;
  const retry = () => { retryDesk(); retryVoices(); };
  /* A code is only the producer's when the producer's own body carried it.
     A status the transport reported and a refusal this page derived from the
     returned records are announced as what they are. */
  const readFailure = routeFailure || timeoutFailure || [desk, voices].find((result) => result.state === 'error');
  const failure = invalid
    ? {origin: 'derived', code: g.reason}
    : {origin: failureOrigin(readFailure?.code), code: readFailure?.code || 'unstated'};

  const bridges = g.creators.filter((c) => g.bridge.get(c.graphKey));
  const topicVoices = (topic) => g.edges.filter((e) => e.topicKey === topic.graphKey).length;
  const topicMeta = (topic) => [MOM_META[topic.momentum] ? MOM_META[topic.momentum].label : null, plural(topicVoices(topic), 'voice')].filter(Boolean).join(' · ');
  const creatorMeta = (creator) => engagementWords(creator.reach) + ' · ' + plural((g.creatorTopics.get(creator.graphKey) || []).length, 'topic');
  /* The plot is as tall as its longest column needs at the measured row
     pitch, so two topics do not sit half a screen apart. */
  const plotRows = Math.max(g.topics.length, g.creators.length);
  const plotHeight = Math.max(240, Math.ceil((plotRows - 1) * rowPitch / 0.8));
  const recordedLinks = (Array.isArray(deskData.bridges) ? deskData.bridges : []).filter(row => recordMarket(row?.mk) && normHandle(row?.h) && ['from', 'to'].every(field => typeof row[field] === 'string' && row[field].trim()));
  /* Quiet register, 23 Sept 2026: recorded links are a list separated by
     hairlines, not framed cards, and the note under the heading is in plain
     words rather than the producer's terms. */
  const recordedSection = ready && recordedLinks.length > 0 ? <section className="net-recorded-links">
    <h2>Recorded links across scenes</h2>
    <p>These links come from a separate 14-day collection, so their exact dates and source posts are not shown here. Each handle opens its profile.</p>
    <ul className="net-link-list">
      {recordedLinks.map((row, index) => <li key={identityKey(recordMarket(row.mk), normHandle(row.h)) + index}><a className="net-bridge-card" href={creatorHref(row.h)}>
        <span className="net-bridge-name">@{normHandle(row.h)}</span>
        <span className="net-topic-tag">{row.from} / {row.to}</span>
        <span className="net-provenance">{regionLabel(recordMarket(row.mk))} · 14-day collection</span>
      </a></li>)}
    </ul>
  </section> : null;

  return (
    <div className="network-page" data-network-state={!loading && unavailable ? 'unavailable' : loading ? 'loading' : errored ? 'error' : stale ? 'stale' : held ? 'unavailable' : g.edges.length ? 'ready' : 'empty'}>
      {/* Quiet register, 23 Sept 2026: a page title at the page title size
          and a one sentence lead that names the market, with no eyebrow. Shell
          consistency, 2 October 2026: the title is the menu's noun. */}
      <header className="reveal net-page-head">
        <h1 className="net-page-title">Network</h1>
        <p className="net-lead">Topics and voices: who the latest briefs name under each topic in {marketLabel}, matched to their own posting records.</p>
      </header>

      {loading && (
        <div style={{marginTop: '24px'}}><StateView {...routeStateView(NETWORK_WAIT)} /></div>
      )}

      {held && (
        <div style={{marginTop: '24px'}}><StateView {...routeStateView({state: 'stale', actions: [tryAgainAction(retry)]}, runFacts)} /></div>
      )}

      {!loading && errored && (
        <div className="intel-down" role="alert">
          <p className="id-head">{unavailable ? 'Network is not available yet.' : timedOut ? 'The network read timed out.' : invalid ? g.reason === 'scope_mismatch' ? 'The returned records do not match the selected market.' : 'The returned topic or voice records could not be verified.' : 'The network could not load. ' + (desk.message || voices.message || '')}</p>
          <p className="id-msg">{unavailable ? 'Network data is not available in this version. No relationships were read.' : timedOut ? 'The reads did not answer within 30 seconds. No relationships were drawn.' : 'No relationship is drawn and no count is shown. This does not establish that the market holds none.'}</p>
          {/* Shell consistency, 2 October 2026: the failed read is the one
              empty state pattern, a bold title, one sentence and one bordered
              action, with the origin label and code waiting under a closed
              Details line after it, as on Ask, Fieldwork and Historical. A read
              that may answer again offers Try again; a feature this version
              does not have offers the page that does carry the posts. */}
          {unavailable
            ? <a className="legacy-action" href="#/pulse">Open Today</a>
            : <button type="button" className="legacy-action" onClick={retry}>Try again</button>}
          <details className="workspace-reason"><summary>Details</summary><p className="id-msg">{unavailable ? <>The app returned <code>{routeFailure.code}</code>: {routeFailure.message}</> : timedOut ? <>This page stopped waiting after 30 seconds: <code>{timeoutFailure.code}</code></> : <>{FAILURE_ORIGIN_LABEL[failure.origin]} <code>{failure.code}</code></>}</p></details>
        </div>
      )}

      {!held && !g.edges.length && recordedSection}
      {ready && !g.edges.length && <div className={recordedSection ? 'net-empty-note' : undefined}>
        <EmptyState
          loader={recordedSection ? false : 'orbit'}
          isLoading={false}
          title={recordedSection ? 'No graph matches in the latest briefs' : 'No matching links in the returned records'}
          body="The supplied topic references and eligible voice records do not produce a match. Missing or conflicting identities are withheld. Relationships outside these returned records remain unknown."
          status={networkEmptyFacts({
            market: reg,
            marketLabel,
            topics: Array.isArray(deskData.topics) ? deskData.topics.length : null,
            voices: Array.isArray(voicesData.creators) ? voicesData.creators.length : null,
            days: null,
          })}
          actions={[
            typeof setRegion === 'function' ? {
              label: 'See ' + regionLabel(fallbackMarket(reg)) + ' instead →',
              primary: true,
              onClick: () => setRegion(fallbackMarket(reg)),
            } : null,
            {label: 'Explore the seed graph', href: '#/seedpath'},
          ]}
        />
      </div>}

      {ready && <div className="net-coverage" data-network-coverage>
        {!!g.edges.length && <p>{coverageWords(g.coverage)}</p>}
        {g.coverage.limited && <p>The display is limited to seven connected topics and twelve connected voices.</p>}
        <details><summary>How these links are matched</summary>
        <p>Received {g.coverage.receivedTopics} topic records and {g.coverage.receivedCreators} voice records. Excluded {g.coverage.invalidTopicRows + g.coverage.invalidCreatorRows} records with missing identities and {g.coverage.ambiguousTopics + g.coverage.ambiguousCreators} identities with conflicting or incomplete records.</p>
        <p>Matches require the same recorded market. Voice markets reflect the largest share of their source records. Topic references omit platform identity, and voice handles can aggregate multiple accounts. Creator links open the combined handle profile.</p>
        <p>Voice ranking uses capped summed engagement from records collected over a rolling 30-day window, with missing engagement counted as zero by the producer. These are cached, limited results. A shared observation window for the associations is unavailable.</p>
        {typeof deskData.updated === 'string' && <p>Supplied desk update: {deskData.updated}.</p>}
        </details>
      </div>}

      {ready && !!g.edges.length && (
        <>
          {/* Quiet register, 23 Sept 2026: the plot states its finding and
              its source, draws neutral links on an open page, and labels
              every node with its values. Red marks only the node in hand and
              its links. */}
          <div className="reveal net-canvas">
            <h2 className="net-finding">{networkFinding(g.creators.length, bridges.length)}</h2>
            <p className="net-source">Topics from the latest briefs for {marketLabel}; voices ranked by capped engagement over a rolling 30-day window.</p>
            <div className="net-column-heads"><span>Topics</span><span>Voices, by engagement</span></div>
            <div className="net-plot" ref={plot} style={{height: plotHeight}}>
            <svg className="net-svg" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
              {g.edges.map((e) => {
                const inHand = Boolean(activeNode) && (activeNode.type === 'topic' ? e.topicKey === activeNode.id : e.creatorKey === activeNode.id);
                return (
                  <path
                    key={e.key}
                    className="net-edge"
                    d={edgePath(e.y1, e.y2)}
                    fill="none"
                    stroke={inHand ? 'var(--accent-text)' : 'var(--ink-2)'}
                    strokeWidth={inHand ? 2 : 1.2}
                    vectorEffect="non-scaling-stroke"
                    opacity={!activeNode ? e.opacity : inHand ? 1 : 0.08}
                  />
                );
              })}
            </svg>

            {g.topics.map((t) => (
              <a
                key={t.graphKey}
                href={buildTopicHash(t.id, t.region)}
                className="net-node net-node--topic"
                data-graph-key={t.graphKey}
                style={{top: g.topicRow.get(t.graphKey) + '%'}}
                onMouseEnter={() => setHoveredNode({type: 'topic', id: t.graphKey})}
                onMouseLeave={() => setHoveredNode(null)}
                onFocus={() => setFocusedNode({type: 'topic', id: t.graphKey})}
                onBlur={() => setFocusedNode(null)}
              >
                <span className="net-node-label">{t.topic || t.label}</span>
                <span className="net-node-meta"><span className="net-node-market">{regionLabel(t.region)}</span> · {topicMeta(t)}</span>
              </a>
            ))}

            {g.creators.map((c) => {
              const on = g.bridge.get(c.graphKey);
              return (
                <a
                  key={c.graphKey}
                  href={creatorHref(c.handle)}
                  className="net-node net-node--creator"
                  data-graph-key={c.graphKey}
                  style={{top: g.creatorRow.get(c.graphKey) + '%'}}
                  onMouseEnter={() => setHoveredNode({type: 'creator', id: c.graphKey})}
                  onMouseLeave={() => setHoveredNode(null)}
                  onFocus={() => setFocusedNode({type: 'creator', id: c.graphKey})}
                  onBlur={() => setFocusedNode(null)}
                >
                  <span className="net-creator-label">@{c.handle}</span>
                  <span className={'net-node-meta' + (on ? ' net-bridge-label' : '')}><span className="net-node-market">{regionLabel(c.market)}</span> · {creatorMeta(c)}</span>
                </a>
              );
            })}
            </div>
            <div className="net-mobile-links">
              {g.topics.map((topic) => (
                <section className="net-topic-links" key={topic.graphKey} data-graph-key={topic.graphKey}>
                  <h2><a href={buildTopicHash(topic.id, topic.region)}>{topic.topic || topic.label} · {regionLabel(topic.region)}</a></h2>
                  <p className="net-node-meta">{topicMeta(topic)}</p>
                  <ul>{g.creators.filter((creator) => (g.creatorTopics.get(creator.graphKey) || []).some((linked) => linked.graphKey === topic.graphKey)).map((creator) => (
                    <li key={creator.graphKey}><a href={creatorHref(creator.handle)}><span className="net-creator-label">@{creator.handle}</span><span className={'net-node-meta' + (g.bridge.get(creator.graphKey) ? ' net-bridge-label' : '')}>{regionLabel(creator.market)} · {creatorMeta(creator)}</span></a></li>
                  ))}</ul>
                </section>
              ))}
            </div>
          </div>

          {/* Quiet register, 23 Sept 2026: the voices that link topics are a
              list under a plain heading, not a red marker and framed cards. */}
          {!!bridges.length && (
            <section className="net-bridges-section">
              <h2>Voices that link more than one topic</h2>
              <ul className="net-link-list">
                {bridges.map((c) => (
                  <li key={c.graphKey}>
                    <a href={creatorHref(c.handle)} className="net-bridge-card">
                      <span className="net-bridge-name">@{c.handle}</span>
                      <span className="net-topic-tag">{topicWords(g.creatorTopics.get(c.graphKey) || [])}</span>
                      <span className="net-provenance-value">{regionLabel(c.market)} · {engagementWords(c.reach)} over a rolling 30-day window</span>
                    </a>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
      {!stale && !!g.edges.length && recordedSection}
    </div>
  );
}
