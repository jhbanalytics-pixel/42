/* PULSE · topic story (full page), premium dark editorial.
   The desk brief (Prompt Pulse, gen-z voices, receipts, platforms, creators)
   merged with live aggregates: reach, the observed-volume momentum line, the
   share-of-voice line, the slang it travels in, and the post wall. Share of
   voice is a relative per-market %; momentum is observed volume, not a forecast.
   Markup and styling track the design prototype; every figure still comes from
   the API payload, absent fields drop. */
import {useState, useEffect} from 'react';
import {useApi, apiGetFresh} from './api.js';
import {go} from './router.js';
import {hasBriefContent} from './briefText.js';
import {readTopicPins, changeTopicPins, sourceTopicPin, topicPinKey, clearTopicPrior} from './topicPins.js';
import {Icon, MOM_META, Sentiment, Gloss} from './parts.jsx';
import {countOf, decorate, human, readerWord, webHref} from './model.js';
import {BigChart} from './charts.jsx';
import {CopyBtn, CopyBriefBtn, RefineBar} from './views.jsx';
import {PlatformGlyph, PageHero, MetaRail, MarketChip, EmptyState, Skeleton} from './ui/index.js';
import './styles/topic.css';

const PP_STEPS = [
  ['Trend', 'trend'],
  ['Relevance', 'relevance'],
  ['Opportunity', 'opportunity'],
  ['Idea', 'idea'],
  ['Prompt', 'prompt'],
];

/* shared section header: a short accent tick, a label, a rule, and a meta note.
   Quiet register, 23 Sept 2026: the heading is the sans in sentence case with
   no tracking, and the note is a muted label at 14px, not spaced mono capitals. */
function SectionHead({label, note}){
  return (
    <div style={{display: 'flex', alignItems: 'center', gap: '14px', margin: '40px 0 0', flexWrap: 'wrap'}}>
      <span style={{fontFamily: 'var(--sans)', fontSize: 'var(--type-4)', fontWeight: 600, color: 'var(--ink)'}}>
        <span style={{display: 'inline-block', width: '16px', height: '2px', background: 'var(--accent)', marginRight: '12px', verticalAlign: 'middle'}} />{label}
      </span>
      <span style={{flex: 1, height: '1px', background: 'var(--line)', minWidth: '20px'}} />
      {note && <span style={{...LABEL, fontVariantNumeric: 'tabular-nums'}}>{note}</span>}
    </div>
  );
}

const CARD = {border: '1px solid var(--line)', background: 'var(--surface)'};
/* A label on the topic page: the sans at 14px in muted, sentence case written
   at the source, no tracking. */
const LABEL = {fontFamily: 'var(--sans)', fontSize: 'var(--type-3)', color: 'var(--muted)', fontWeight: 400};
/* Quiet register, 23 Sept 2026: counts, ages and measurements are not ids, so
   they are the sans at 14px with tabular numerals rather than 11px mono. */
const FIGURE = {...LABEL, fontVariantNumeric: 'tabular-nums'};

/* one post on the wall: text clamps to six lines, click expands it in place, a
   cross-tag chip flags posts that also live in another topic, and "view
   original" links out when the source post carries a url. */
export function WallCard({p}){
  const [open, setOpen] = useState(false);
  const slug = String(p.handle || '').replace(/^@+/, '');
  const href = webHref(p.url);
  const also = Array.isArray(p.also) ? p.also : [];
  const clamp = open ? {} : {display: '-webkit-box', WebkitLineClamp: 6, WebkitBoxOrient: 'vertical', overflow: 'hidden'};
  return (
    <div style={{...CARD, padding: '16px 18px', display: 'flex', flexDirection: 'column', gap: '10px'}}>
      <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px'}}>
        <span style={LABEL}>{[readerWord(p.platform), p.market ? String(p.market).toUpperCase() : ''].filter(Boolean).join(' · ')}</span>
        {p.age && <span style={FIGURE}>{p.age}</span>}
      </div>
      {also.length > 0 && (
        <span style={{...LABEL, alignSelf: 'flex-start'}}>Also in {also.join(', ')}</span>
      )}
      <p onClick={() => setOpen(!open)} title={open ? 'Show less' : 'Show full post'} style={{fontFamily: 'var(--serif)', fontSize: 'var(--type-4)', lineHeight: 1.5, color: 'var(--ink)', margin: 0, textWrap: 'pretty', cursor: 'pointer', ...clamp}}>{p.text}</p>
      <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px', marginTop: 'auto', paddingTop: '10px', borderTop: '1px solid var(--hairline)', flexWrap: 'wrap', minWidth: 0}}>
        {p.handle
          ? <a className="topic-link" href={'#/creator/' + encodeURIComponent(slug)} style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-3)', color: 'var(--accent-text)', fontWeight: 600}}>{p.handle}</a>
          : <span style={LABEL}>{readerWord(p.platform)}</span>}
        <span style={{display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', minWidth: 0}}>
          {href && <a className="topic-link" href={href} target="_blank" rel="noopener noreferrer" style={{fontFamily: 'var(--sans)', fontSize: 'var(--type-3)', color: 'var(--accent-text)', fontWeight: 600}}>View original</a>}
          <span style={FIGURE}>{human(p.engagement)} engagement</span>
        </span>
      </div>
    </div>
  );
}

export function TopicStory({id, region, session, onAuth, scopeError}){
  const reg = String(region || 'ALL').toLowerCase();
  const [data] = useApi('/api/topic/' + encodeURIComponent(id) + '?region=' + reg, session, onAuth, !!id && !scopeError);
  const [override, setOverride] = useState(null);
  const [opp, setOpp] = useState('');
  const [oppLoading, setOppLoading] = useState(false);
  const [watch, setWatch] = useState(readTopicPins);
  const [historyClearError, setHistoryClearError] = useState(false);

  useEffect(() => { setOverride(null); }, [id, region]);
  /* A topic link with no id has nothing to fetch: Discover is where a topic is
     chosen, so the route hands the reader there instead of asking the API for
     a 404 and painting an error. */
  useEffect(() => { if (!id && !scopeError) go('/explore'); }, [id, scopeError]);

  const raw = override || (data.state === 'ready' ? data.data : null);
  const t = raw ? decorate(raw) : null;

  /* lazy opportunity lens, same contract the panel used: engine-tracked briefs
     fetch it on open when they ship without one */
  useEffect(() => {
    setOpp(''); setOppLoading(false);
    if (!t || !hasBriefContent(t.brief) || t.brief.opportunity || t.generated) return;
    let live = true;
    setOppLoading(true);
    apiGetFresh('/api/desk/opportunity/' + encodeURIComponent(t.id) + '?region=' + encodeURIComponent(reg))
      .then((d) => { if (!live) return; setOppLoading(false); if (d && d.opportunity) setOpp(d.opportunity); })
      .catch(() => { if (live) setOppLoading(false); });
    return () => { live = false; };
  }, [t && t.id]);

  if (scopeError) return <div className="page topic"><EmptyState title="Invalid topic link" body="The link needs one valid market selection. Choose a market in the header or return to Discover." actions={[{label: 'Open Discover', href: '#/explore'}]} /></div>;
  if (!id || data.state === 'loading' || data.state === 'idle'){
    return <div className="page"><div className="wrap"><Skeleton variant="hero" railRows={5} /></div></div>;
  }
  if (data.state === 'error'){
    const missing = /404/.test(data.message || '');
    return (
      <div className="page"><div className="wrap">
        <a className="cp-back" href="#/explore"><Icon.arrRight className="ic flip" /> Discover</a>
        <EmptyState
          title={missing ? 'No signal for this topic' : 'The topic could not load'}
          body={missing ? 'No signal for this topic in the last 30 days.' : (data.message || 'Something went wrong.')}
          cta={{label: 'Try again', onClick: () => window.location.reload()}}
        />
      </div></div>
    );
  }
  if (!t) return null;

  const color = MOM_META[t.momentum] ? MOM_META[t.momentum].color : 'var(--accent)';
  const momLabel = MOM_META[t.momentum] ? MOM_META[t.momentum].label : 'Steady';
  const mkt = String(t.region || '').toUpperCase();
  const pin = sourceTopicPin(raw);
  const on = !!pin && watch.pins.some(saved => topicPinKey(saved) === topicPinKey(pin));
  const toggleWatch = () => {
    if (!pin) return;
    const next = changeTopicPins(pins => {
      const exists = pins.some(saved => topicPinKey(saved) === topicPinKey(pin));
      return on ? exists ? pins.filter(saved => topicPinKey(saved) !== topicPinKey(pin)) : pins : exists ? pins : [...pins, pin];
    });
    setWatch(next);
    if (on && next.status === 'ready') setHistoryClearError(!clearTopicPrior(pin));
  };

  const brief = hasBriefContent(t.brief) ? t.brief : null;
  /* no hollow cards: empty idea text or an all-empty prompt drops the slot */
  const idea = brief && brief.idea && brief.idea.text ? brief.idea : null;
  const prompt = brief && brief.prompt && (brief.prompt.nano || brief.prompt.lyria) ? brief.prompt : null;
  const oppText = brief ? (brief.opportunity || opp) : '';
  const voices = Array.isArray(t.voices) ? t.voices : [];
  /* The url column (index 5) of a receipt tuple is collected data; only a web address stays a link. */
  const receipts = (Array.isArray(t.receipts) ? t.receipts : []).map((r) => (Array.isArray(r) ? r.map((cell, index) => (index === 5 ? webHref(cell) : cell)) : r));
  const platforms = Array.isArray(t.platforms) ? t.platforms : [];
  const peakSrc = platforms.length ? Math.max(...platforms.map((p) => p[1])) : 1;
  const creators = Array.isArray(t.creators_list) ? t.creators_list : [];
  const tags = Array.isArray(t.tags) ? t.tags : [];
  const slang = Array.isArray(t.slang) ? t.slang : [];
  const wall = Array.isArray(t.wall) ? t.wall : [];
  const vol = Array.isArray(t.volume_series) ? t.volume_series : [];
  const sov = Array.isArray(t.sov_series) ? t.sov_series : [];

  const chg = typeof t.change === 'number' ? t.change : null;
  const chgColor = chg !== null ? (chg >= 0 ? 'var(--up)' : 'var(--down)') : 'var(--faint)';
  const chgTxt = chg !== null ? (chg >= 0 ? '▲ ' : '▼ ') + Math.abs(chg) + '%' : '';

  /* instrument readouts for the dossier charts. Every segment is built from
     real props; anything absent is omitted rather than invented. */
  const momTone = (t.momentum === 'rising' || t.momentum === 'building') ? 'var(--up)'
    : t.momentum === 'cooling' ? 'var(--down)' : 'var(--muted)';
  const volValue = typeof t.reach === 'number' ? human(t.reach)
    : (vol.length ? String(Math.round(vol[vol.length - 1].n)) : null);
  const volReadout = {
    eyebrow: 'Signal trend',
    value: volValue,
    delta: chg !== null ? chgTxt : null,
    deltaColor: chgColor,
    meta: [typeof t.reach === 'number' ? 'Engagement' : 'Tracked volume', '30d', mkt || null].filter(Boolean).join(' · '),
    tag: <span className="bc-ro-pill" style={{color: momTone}}>{momLabel}</span>,
  };
  const sovVals = sov.map((p) => p.sov_pct).filter((v) => typeof v === 'number');
  const sovNow = typeof t.sov === 'number' ? t.sov : (sovVals.length ? sovVals[sovVals.length - 1] : null);
  const sovReadout = {
    eyebrow: 'Share of voice',
    value: sovNow !== null ? sovNow.toFixed(1) : null,
    unit: sovNow !== null ? '%' : null,
    meta: sovVals.length ? Math.min(...sovVals).toFixed(1) + '% to ' + Math.max(...sovVals).toFixed(1) + '% · 30d' : null,
    tag: <span className="bc-ro-tag">Relative</span>,
  };

  const railItems = [
    {value: human(t.reach), label: 'Engagement · 30d · ' + mkt},
    {value: typeof t.sov === 'number' ? t.sov.toFixed(1) + '%' : null, label: 'Share of voice · ' + mkt},
    {value: human(t.mentions), label: 'Mentions · 30d · ' + mkt},
    {value: momLabel, label: 'Momentum · observed', tone: {rising: 'up', building: 'up', steady: 'neutral', cooling: 'down'}[t.momentum] || 'neutral'},
    // Seed score: how worth-seeding the trend is for driving Nano Banana / Lyria,
    // shown only when the engine has scored it.
    ...(typeof t.seed === 'number'
      ? [{value: Math.round(t.seed * 100) + '%', label: 'Seed score · Nano Banana / Lyria fit', tone: 'accent'}]
      : []),
  ];

  const heroEyebrow = (
    <span style={{display: 'inline-flex', alignItems: 'center', gap: '9px'}}>
      {mkt && <MarketChip market={mkt} />}
      {t.label}{t.regionName ? ' · ' + t.regionName : ''}
    </span>
  );
  const heroSub = t.why ? <Gloss text={t.why} /> : null;
  const heroActions = (
    <>
      <button aria-pressed={on} disabled={!pin || historyClearError} onClick={toggleWatch} className={'legacy-action' + (on ? ' legacy-action--primary' : '')} style={{gap: '7px'}}>
        <svg viewBox="0 0 24 24" width="12" height="12" fill={on ? 'var(--accent-ink)' : 'none'} stroke="currentColor" strokeWidth="2" strokeLinejoin="round"><path d="m12 3.5 2.6 5.4 5.9.8-4.3 4.1 1 5.9L12 16.9l-5.2 2.8 1-5.9-4.3-4.1 5.9-.8z" /></svg>
        {on ? 'Watching this topic' : 'Watch topic'}
      </button>
      {!pin && <span>Choose a recorded market before saving this topic.</span>}
      {watch.status === 'unavailable' && <span role="alert">Topic pins could not be saved or read. Existing saved data has been preserved.</span>}
      {historyClearError && <span role="alert">The pin was removed, but its previous reading could not be cleared. <button className="legacy-action" onClick={() => setHistoryClearError(!clearTopicPrior(pin))}>Retry clearing previous reading</button></span>}
      {brief && <CopyBriefBtn t={t} />}
      {brief && <button className="copy-brief" onClick={() => window.print()}>PDF</button>}
    </>
  );

  return (
    <div className="page topic">
      <div className="wrap" style={{paddingTop: '24px', paddingBottom: '90px'}}>
        {/* breadcrumb */}
        <a className="cp-back" href="#/explore" style={{gap: '8px', marginBottom: '18px'}}>
          {'←'} Discover<span style={{color: 'var(--faint)'}}>/</span><span style={{color: 'var(--accent-text)'}}>{t.topic}</span>
        </a>

        {/* hero: editorial column sized to measure, instrument aside fills the leftover width */}
        <div className="reveal topic-hero">
          <div className="page-hero-body" style={{minWidth: 0}}>
            <PageHero
              eyebrow={heroEyebrow}
              title={<span className="topic-hero-title">{t.topic}</span>}
              sub={null}
              actions={null}
            />
            {chg !== null && (
              <div style={{display: 'flex', alignItems: 'center', gap: '12px', margin: '14px 0 16px', flexWrap: 'wrap'}}>
                <span style={{fontFamily: 'var(--sans)', fontSize: 'var(--type-6)', fontWeight: 600, fontVariantNumeric: 'tabular-nums', color: chgColor}}>{chgTxt}</span>
                <span style={LABEL}>Over 30 days</span>
              </div>
            )}
            {heroSub && <p className="ui-page-hero-sub" style={{margin: '0 0 22px'}}>{heroSub}</p>}
            <div style={{display: 'flex', gap: '9px', flexWrap: 'wrap'}}>{heroActions}</div>
          </div>

          {/* instrument aside: fills leftover width, kills the dead gap */}
          {railItems.some((it) => it && it.value !== null && it.value !== undefined && it.value !== '') && (
            <aside className="topic-instrument">
              <div className="topic-instrument-cap">Signal at a glance · 30d · {mkt}</div>
              <MetaRail items={railItems} />
            </aside>
          )}
        </div>

        {/* Plain-language explainer for the seed score, so a reader meets the
            term in context the first time they see the number. */}
        {typeof t.seed === 'number' && (
          <p style={{fontFamily: 'var(--sans)', fontSize: 'var(--type-3)', lineHeight: 1.5, color: 'var(--muted)', margin: '10px 2px 0', maxWidth: 'min(72ch, 100%)'}}>
            <b style={{color: 'var(--ink-2)', fontWeight: 600}}>Seed score</b> reads how ready this trend is to spin into a Nano Banana (image) or Lyria (audio) activation, separate from how hot it is. Higher is a stronger seed, and a quiet trend can still seed well.
          </p>
        )}

        {/* the read + the move */}
        {(t.read || t.angle) && (
          <div className="reveal m-stack" style={{marginTop: '18px', border: '1px solid var(--line)', background: 'var(--surface)', overflow: 'hidden', display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)'}}>
            <div style={{padding: '20px 24px', borderRight: '1px solid var(--hairline)'}}>
              <div style={{...LABEL, marginBottom: '10px'}}>The read</div>
              <p style={{fontSize: 'var(--type-3)', lineHeight: 1.6, color: 'var(--ink)', margin: 0, textWrap: 'pretty'}}>{t.read || '-'}</p>
            </div>
            <div style={{padding: '20px 24px', background: 'var(--surface-2)'}}>
              <div style={{...LABEL, color: 'var(--accent-text)', marginBottom: '10px'}}>The move · so what</div>
              <p style={{fontSize: 'var(--type-3)', lineHeight: 1.6, color: 'var(--ink)', margin: 0, textWrap: 'pretty'}}>{t.angle || '-'}</p>
            </div>
          </div>
        )}

        {/* charts */}
        {(vol.length > 1 || sov.length > 1) && (
          <div className="ts-charts m-stack" style={{display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: '14px', marginTop: '18px'}}>
            {vol.length > 1 && (
              <div style={{...CARD, padding: '20px 22px 16px'}}>
                <BigChart label="Tracked volume · by collection date" values={vol.map((p) => p.n)} dates={vol.map((p) => p.date)} format={(v) => String(Math.round(v))} color={color} height={290}
                  readout={volReadout} caption="Observed daily volume · not a forecast" />
              </div>
            )}
            {sov.length > 1 && (
              <div style={{...CARD, padding: '20px 22px 16px'}}>
                <BigChart label="Share of voice · 30d" values={sov.map((p) => p.sov_pct)} dates={sov.map((p) => p.date)} format={(v) => v.toFixed(0) + '%'} dashed height={290}
                  readout={sovReadout} caption="Relative per market · the board sums to 100%" />
              </div>
            )}
          </div>
        )}

        {/* cited voices */}
        {voices.length > 0 && (
          <>
            <SectionHead label="Cited voices from the feed" note={'Direct source excerpts · ' + mkt} />
            <div style={{marginTop: '18px', ...CARD, overflow: 'hidden'}}>
              {voices.map((g, i) => (
                <div key={i} style={{display: 'grid', gridTemplateColumns: 'auto 1fr auto', gap: '14px', alignItems: 'baseline', padding: '14px 20px', borderBottom: i < voices.length - 1 ? '1px solid var(--hairline)' : 'none'}}>
                  <span style={{...LABEL, whiteSpace: 'nowrap'}}>{readerWord(g[0])}</span>
                  <span style={{fontFamily: 'var(--serif)', fontSize: 'var(--type-4)', lineHeight: 1.5, color: 'var(--ink)', textWrap: 'pretty'}}>{'“'}<Gloss text={g[1]} />{'”'}</span>
                  {g[2] ? <span style={{...FIGURE, whiteSpace: 'nowrap'}}>{g[2]}</span> : <span />}
                </div>
              ))}
            </div>
          </>
        )}

        {/* prompt pulse */}
        {brief && <>
          <div className="pp-intro"><span>Signal brief</span><span className="ln" /></div>
          <div className="pp-goal">North star · turn this signal into participation with <b>Nano Banana</b> &amp; <b>Lyria</b></div>
          <div className="pp">
            {PP_STEPS.map(([label, key], idx) => {
              if (key === 'idea' && !idea) return null;
              if (key === 'prompt' && !prompt) return null;
              if (key === 'opportunity'){ if (!oppText && !oppLoading) return null; }
              else if (key !== 'idea' && key !== 'prompt' && !brief[key]) return null;
              return (
                <div className="pp-step" key={key}>
                  <div className="pp-num"><span className="n">{idx + 1}</span></div>
                  <div>
                    <div className="pp-k">The {label}</div>
                    {key === 'idea' ? (
                      <>
                        <span className="pp-tool">{idea.tool === 'Lyria' ? <Icon.music className="ic" /> : <Icon.image className="ic" />} {idea.tool === 'Nano' ? 'Nano Banana' : idea.tool}</span>
                        <div className="pp-v">{idea.text}</div>
                      </>
                    ) : key === 'prompt' ? (
                      <>
                        {prompt.nano && <div className="prompt-box"><div className="pb-k"><span>Nano Banana · image</span><CopyBtn text={prompt.nano} /></div><div className="pb-v">{prompt.nano}</div></div>}
                        {prompt.lyria && <div className="prompt-box"><div className="pb-k"><span>Lyria · music</span><CopyBtn text={prompt.lyria} /></div><div className="pb-v">{prompt.lyria}</div></div>}
                      </>
                    ) : key === 'opportunity' ? (
                      oppText ? <div className="pp-v">{oppText}</div> : <div className="pp-v opp-shimmer" aria-hidden="true"><span /><span /></div>
                    ) : (
                      <div className="pp-v">{brief[key]}</div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </>}

        {t.generated && <RefineBar t={t} onUpdate={setOverride} />}

        {/* receipts */}
        {receipts.length > 0 && (
          <>
            <SectionHead label="The receipts · top signal sources" note={countOf(receipts.length, 'source') + ' · 30 days'} />
            <div style={{marginTop: '16px', ...CARD, overflow: 'hidden'}}>
              {receipts.map((r, i) => (
                <div key={i} style={{display: 'grid', gridTemplateColumns: 'auto 1fr auto', gap: '14px', alignItems: 'baseline', padding: '13px 20px', borderBottom: i < receipts.length - 1 ? '1px solid var(--hairline)' : 'none'}}>
                  <span style={{...LABEL, whiteSpace: 'nowrap'}}>{readerWord(r[0])}</span>
                  <span style={{fontSize: 'var(--type-3)', color: 'var(--ink-2)', lineHeight: 1.45, textWrap: 'pretty'}}>{r[5] ? <a className="topic-link" href={r[5]} target="_blank" rel="noopener noreferrer" style={{color: 'var(--accent-text)', fontWeight: 600}}>{r[1]}</a> : <b style={{color: 'var(--ink)'}}>{r[1]}</b>} · {r[2]}</span>
                  <span style={{...FIGURE, whiteSpace: 'nowrap'}}>{r[3]}{r[4] ? (r[3] ? ' · ' : '') + r[4] : ''}</span>
                </div>
              ))}
            </div>
          </>
        )}

        {/* where it lives */}
        {platforms.length > 0 && (
          <>
            <SectionHead label="Where it lives" note="Engagement by platform · 30 days" />
            <div style={{marginTop: '16px', ...CARD, padding: '18px 22px', display: 'flex', flexDirection: 'column', gap: '11px'}}>
              {platforms.map(([name, val]) => (
                <div key={name} style={{display: 'grid', gridTemplateColumns: '120px 1fr auto', gap: '14px', alignItems: 'center'}}>
                  <PlatformGlyph platform={name} />
                  <span style={{position: 'relative', height: '7px', background: 'var(--surface-inset)', overflow: 'hidden'}}>
                    <span style={{position: 'absolute', left: 0, top: 0, bottom: 0, width: (val / peakSrc * 100) + '%', background: 'var(--accent)'}} />
                  </span>
                  <span style={{...FIGURE, whiteSpace: 'nowrap'}}>{val}</span>
                </div>
              ))}
            </div>
          </>
        )}

        {/* top voices */}
        {creators.length > 0 && (
          <>
            <SectionHead label="Top voices on it" note="Tap to open profile" />
            <div className="m-stack" style={{display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: '13px', marginTop: '18px'}}>
              {creators.map((c) => {
                const handle = String(c);
                const slug = handle.replace(/^@+/, '');
                return (
                  <a key={handle} className="topic-card-link" href={'#/creator/' + encodeURIComponent(slug)} style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '10px', padding: '16px 18px'}}>
                    <span style={{fontFamily: 'var(--serif)', fontSize: 'var(--type-5)', fontWeight: 600, letterSpacing: '-0.01em', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis'}}>{handle}</span>
                    <Icon.arrRight className="ic" />
                  </a>
                );
              })}
            </div>
          </>
        )}

        {/* how it reads: media tone (news) + social mood (the engine's briefs) */}
        {(typeof t.sentiment === 'number' || t.social_mood) && (
          <div style={{marginTop: '40px', ...CARD, padding: '18px 22px'}}>
            <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '14px', flexWrap: 'wrap', gap: '8px'}}>
              <span style={LABEL}>How it reads · 30 days · {mkt}</span>
              {typeof t.sentiment === 'number' && (
                <span style={FIGURE}>
                  Media tone {(0.5 + t.sentiment / 2).toFixed(2)}{t.velocity ? ' · ' + t.velocity + ' velocity vs today' : ''}{t.age ? ' · last signal ' + t.age + ' ago' : ''}
                </span>
              )}
            </div>
            <div style={{display: 'flex', flexWrap: 'wrap', gap: '24px', alignItems: 'flex-start'}}>
              {typeof t.sentiment === 'number' && (
                <div style={{display: 'flex', flexDirection: 'column', gap: '6px'}}>
                  <span style={LABEL}>Media tone · news</span>
                  <Sentiment s={t.sentiment} />
                </div>
              )}
              {t.social_mood && (
                <div style={{display: 'flex', flexDirection: 'column', gap: '6px'}}>
                  <span style={LABEL}>Social mood · monitored source</span>
                  <span style={{fontFamily: 'var(--sans)', fontSize: 'var(--type-3)', fontWeight: 600, color: 'var(--ink)'}}>{t.social_mood}</span>
                </div>
              )}
            </div>
            {t.media_tone_dist && t.media_tone_dist.n > 0 && (() => {
              const md = t.media_tone_dist;
              const tot = md.n || 1;
              const pp = Math.round((md.positive || 0) / tot * 100);
              const np = Math.round((md.negative || 0) / tot * 100);
              const nu = Math.max(0, 100 - pp - np);
              return (
                <div style={{marginTop: '16px'}}>
                  <span style={LABEL}>Media tone mix · news · {countOf(md.n, 'post')}</span>
                  <div style={{display: 'flex', height: '10px', overflow: 'hidden', background: 'var(--surface-inset)', marginTop: '7px'}}>
                    <span style={{width: pp + '%', background: 'var(--pos)'}} title={'Positive ' + pp + '%'} />
                    <span style={{width: nu + '%', background: 'var(--neu)', opacity: 0.5}} title={'Neutral ' + nu + '%'} />
                    <span style={{width: np + '%', background: 'var(--neg)'}} title={'Negative ' + np + '%'} />
                  </div>
                  <div style={{...FIGURE, display: 'flex', gap: '14px', marginTop: '7px'}}>
                    <span>{pp}% positive</span><span>{nu}% neutral</span><span>{np}% negative</span>
                  </div>
                </div>
              );
            })()}
            <p style={{...LABEL, margin: '14px 0 0', lineHeight: 1.5}}>Media tone comes from news coverage, not social posts. Social mood is a separate monitored-source read and does not measure a population.</p>
          </div>
        )}

        {/* hashtags + slang */}
        {(tags.length > 0 || slang.length > 0) && (
          <div className="m-stack" style={{display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)', gap: '14px', marginTop: '40px'}}>
            {tags.length > 0 && (
              <div style={{...CARD, padding: '18px 20px'}}>
                <div style={{...LABEL, marginBottom: '12px'}}>Driving hashtags</div>
                <div style={{display: 'flex', flexWrap: 'wrap', gap: '8px 16px'}}>
                  {/* Quiet register, 23 Sept 2026: a hashtag is a string a reader copies,
                      so it stays mono, but at 14px as a word with no frame or fill. */}
                  {tags.map((tag) => <span key={tag} style={{fontFamily: 'var(--mono)', fontSize: 'var(--type-3)', color: 'var(--ink-2)'}}>{tag}</span>)}
                </div>
              </div>
            )}
            {slang.length > 0 && (
              <div style={{...CARD, padding: '18px 20px'}}>
                <div style={{...LABEL, marginBottom: '12px'}}>The slang it is carried in · tap to decode</div>
                <div style={{display: 'flex', flexWrap: 'wrap', gap: '7px'}}>
                  {slang.map((term) => (
                    <a key={term} href={'#/lexicon/' + encodeURIComponent(term)} className="legacy-chip">{term}</a>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* the wall */}
        {wall.length > 0 && (
          <>
            <SectionHead label="The wall · real posts" note={countOf(t.wall_count || wall.length, 'post') + ' driving it · 30 days'} />
            <div className="m-stack" style={{display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0, 1fr))', gap: '13px', marginTop: '18px'}}>
              {wall.map((p) => <WallCard key={p.id} p={p} />)}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
