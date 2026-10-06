/* Pick topics manually when skipping behaviour scan. */
import {useEffect, useState} from 'react';
import {apiGetFresh} from './api.js';
import {FLAG, human, topicLabel} from './model.js';

export function TopicPicker({markets, onContinue, onAuth}){
  const mks = (markets && markets.length ? markets : ['za', 'ng', 'ke']).map((m) => String(m).toLowerCase());
  const [rows, setRows] = useState([]);
  const [selected, setSelected] = useState(() => new Set());
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const ctrl = new AbortController();
    setLoading(true);
    setErr('');
    apiGetFresh('/api/market-topics?markets=' + encodeURIComponent(mks.join(',')), ctrl.signal)
      .then((res) => {
        setRows(Array.isArray(res.topics) ? res.topics : []);
        setLoading(false);
      })
      .catch((e) => {
        if (e && e.auth) onAuth && onAuth();
        else setErr(e && e.message ? e.message : 'Could not load topics.');
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [mks.join(','), onAuth]);

  const toggle = (row) => {
    const key = row.market + ':' + row.query_group;
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const submit = () => {
    const approved = rows.filter((r) => selected.has(r.market + ':' + r.query_group)).map((r) => ({
      id: r.market + ':' + r.query_group,
      market: r.market,
      signal_topic: r.query_group,
      behaviour: r.label || topicLabel(r.query_group),
      metric: {post_count: r.post_count, trend_score: r.trend_score},
    }));
    onContinue(approved);
  };

  return (
    <section className="topic-picker" aria-labelledby="topic-picker-title">
      <h2 id="topic-picker-title">Pick topics manually</h2>
      <p className="topic-picker-sub">Skip the behaviour scan and choose signal topics directly from today&apos;s engine board.</p>
      {loading && <p className="topic-picker-meta">Loading topics…</p>}
      {err && <p className="topic-picker-meta">{err}</p>}
      {!loading && !err && (
        <ul className="topic-picker-list">
          {rows.map((r) => {
            const key = r.market + ':' + r.query_group;
            const on = selected.has(key);
            return (
              <li key={key}>
                <button type="button" className={'topic-picker-row' + (on ? ' active' : '')} aria-pressed={on} onClick={() => toggle(r)}>
                  <span className="topic-picker-flag">{FLAG[String(r.market).toUpperCase()] || r.market}</span>
                  <span className="topic-picker-label">{r.label || topicLabel(r.query_group)}</span>
                  <span className="topic-picker-metric">
                    {typeof r.post_count === 'number' ? r.post_count + ' posts' : ''}
                    {typeof r.trend_score === 'number' ? ' · score ' + r.trend_score.toFixed(2) : ''}
                    {r.reach ? ' · ' + human(r.reach) + ' reach' : ''}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
      <button type="button" className="topic-picker-submit" disabled={!selected.size} onClick={submit}>
        Continue with {selected.size || 0} topic{selected.size === 1 ? '' : 's'}
      </button>
    </section>
  );
}

export default TopicPicker;
