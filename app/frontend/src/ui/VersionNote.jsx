/* The version line (11 October 2026): one quiet line at the foot of the
   menu, "42 v2.3.0" with the short build sha beside it, so the team can say
   which release they are on. It opens a small What's new panel read from
   CHANGELOG.md, which the build bundles with VERSION and the sha
   (vite.config.mjs, __F42_RELEASE__). The panel marks the version the page
   runs as This version; "next, not live yet" in a heading is shown only for
   a version above it, so the release that ships the file never calls itself
   unreleased.

   The page is built with the sha f42-api reports as its version, so when the
   server later answers with another sha a different release is live under an
   open page (newer, or a rollback), and a quiet hint offers a reload. The
   built sha is the expectation and the served one only an observation; a
   served value that is not a sha (a local "dev") is never a difference. App
   reads health once as the page loads, which is when the two agree by
   construction, so this line checks only when the reader comes back to the
   tab and on a slow interval, never more often than the gap. */
import {useCallback, useEffect, useId, useMemo, useRef, useState} from 'react';

export const CHECK_GAP_MS = 5 * 60 * 1000;
export const CHECK_EVERY_MS = 15 * 60 * 1000;
const SHA = /^[0-9a-f]{7,40}$/;
const HEADING = /^## (\d+\.\d+\.\d+) \((.+)\)$/;
const UPDATE = /^### (Jobs update) \((.+)\)$/;

export function builtRelease(){
  return typeof __F42_RELEASE__ === 'undefined' ? null : __F42_RELEASE__;
}

/* A heading "## 2.2.0 (11 October 2026)", one line naming the release, then
   one "- " line per change. A "### Jobs update (date)" heading under a
   version opens a dated jobs entry with its own lines. Anything before the
   first heading is the numbering rule and is not shown. */
export function parseChangelog(text){
  const releases = [];
  let into = null;
  for (const raw of String(text || '').split(/\r?\n/)){
    const line = raw.trim();
    const heading = line.match(HEADING);
    if (heading){
      const when = heading[2].charAt(0).toUpperCase() + heading[2].slice(1);
      into = {version: heading[1], when, next: /^next\b/i.test(heading[2]), summary: '', lines: [], updates: []};
      releases.push(into);
      continue;
    }
    const current = releases[releases.length - 1];
    if (!current || !line) continue;
    const update = line.match(UPDATE);
    if (update){
      into = {title: update[1], when: update[2], lines: []};
      current.updates.push(into);
      continue;
    }
    if (line.startsWith('- ')) into.lines.push(line.slice(2).trim());
    else if (into === current && !current.summary) current.summary = line;
  }
  return releases;
}

/* Orders two major.minor.patch versions as numbers. */
export function compareVersions(a, b){
  const left = String(a).split('.').map(Number);
  const right = String(b).split('.').map(Number);
  for (let at = 0; at < 3; at += 1){
    if (left[at] !== right[at]) return left[at] - right[at];
  }
  return 0;
}

/* Two shas name different commits when both are shas and neither is a
   prefix of the other, so a 7 and a 12 character form of one commit agree. */
export function shaDiffers(built, served){
  const mine = String(built || '').trim().toLowerCase();
  const theirs = String(served || '').trim().toLowerCase();
  if (!SHA.test(mine) || !SHA.test(theirs)) return false;
  return !(mine.startsWith(theirs) || theirs.startsWith(mine));
}

function reloadPage(){
  window.location.reload();
}

/* onChange hears whether the live version differs, so the phone bar can
   mark More while the sheet that holds this line is shut. */
export function VersionNote({release = builtRelease(), active = true, gapMs = CHECK_GAP_MS, everyMs = CHECK_EVERY_MS, onReload = reloadPage, onChange}){
  if (!release || !release.version) return null;
  return <VersionLine release={release} active={active} gapMs={gapMs} everyMs={everyMs} onReload={onReload} onChange={onChange} />;
}

function VersionLine({release, active, gapMs, everyMs, onReload, onChange}){
  const [open, setOpen] = useState(false);
  const [stale, setStale] = useState(false);
  const buttonRef = useRef(null);
  const dialogRef = useRef(null);
  const titleId = useId();
  const releases = useMemo(() => parseChangelog(release.changelog), [release.changelog]);
  const short = release.sha ? release.sha.slice(0, 7) : null;

  const close = useCallback(({returnFocus = true} = {}) => {
    const dialog = dialogRef.current;
    if (dialog && dialog.open) dialog.close();
    setOpen(false);
    if (returnFocus && buttonRef.current) buttonRef.current.focus();
  }, []);

  const show = () => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    setOpen(true);
  };

  /* The phone More sheet can shut under an open panel (a route change); a
     modal left open inside a hidden sheet would block the page unseen. */
  useEffect(() => {
    if (!active) close({returnFocus: false});
  }, [active, close]);

  useEffect(() => {
    if (typeof onChange === 'function') onChange(stale);
  }, [stale, onChange]);

  useEffect(() => {
    if (!release.sha) return undefined;
    let live = true;
    let last = Date.now();
    const check = () => {
      if (document.hidden) return;
      const now = Date.now();
      if (now - last < gapMs) return;
      last = now;
      fetch('/api/health', {cache: 'no-store'})
        .then((response) => (response && response.ok ? response.json() : null))
        .then((body) => {
          if (live && body && SHA.test(String(body.version || '').trim().toLowerCase())) setStale(shaDiffers(release.sha, body.version));
        })
        .catch(() => {});
    };
    const timer = window.setInterval(check, everyMs);
    document.addEventListener('visibilitychange', check);
    return () => {
      live = false;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', check);
    };
  }, [release.sha, gapMs, everyMs]);

  /* Escape is the panel's alone: the rail would also take it to shut More. */
  const onKeyDown = (event) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    event.stopPropagation();
    close();
  };
  const onCancel = (event) => {
    event.preventDefault();
    close();
  };
  /* The panel has no padding of its own, so a click that lands on the
     dialog itself is a click on the backdrop. */
  const onBackdrop = (event) => {
    if (event.target === dialogRef.current) close();
  };

  /* The accessible name starts with the visible text, so a reader who says
     what they see reaches the line. */
  return (
    <div className="rail42-version">
      <button
        ref={buttonRef}
        type="button"
        className="rail42-version-button"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={show}
      >
        <span className="rail42-version-name">42 v{release.version}</span>
        {short ? <>{' '}<span className="rail42-version-sha">{short}</span></> : null}
        <span className="sr-only">. What's new</span>
      </button>
      <div className="rail42-version-status" role="status">
        {stale ? <button type="button" className="rail42-version-reload" onClick={() => onReload()}>Version changed, reload</button> : null}
      </div>
      <dialog
        ref={dialogRef}
        className="rail42-whatsnew"
        aria-labelledby={titleId}
        onKeyDown={onKeyDown}
        onCancel={onCancel}
        onClose={() => setOpen(false)}
        onClick={onBackdrop}
      >
        <div className="rail42-whatsnew-body">
          <div className="rail42-whatsnew-head">
            <h2 className="rail42-whatsnew-title" id={titleId}>What's new</h2>
            <button type="button" className="rail42-whatsnew-close" onClick={() => close()}>Close</button>
          </div>
          <p className="rail42-whatsnew-on">You are on 42 v{release.version}{short ? `, build ${short}` : ''}.</p>
          <ol className="rail42-whatsnew-list">
            {releases.map((entry) => {
              const order = compareVersions(entry.version, release.version);
              const when = entry.next ? (order > 0 ? entry.when : null) : entry.when;
              return (
                <li key={entry.version} className="rail42-whatsnew-release" data-current={order === 0 ? '' : undefined}>
                  <h3 className="rail42-whatsnew-heading">
                    <span className="rail42-whatsnew-version">{entry.version}</span>
                    {when ? <span className="rail42-whatsnew-when">{when}</span> : null}
                    {order === 0 ? <span className="rail42-whatsnew-current">This version</span> : null}
                  </h3>
                  {entry.summary ? <p className="rail42-whatsnew-summary">{entry.summary}</p> : null}
                  <ul className="rail42-whatsnew-lines">{entry.lines.map((line) => <li key={line}>{line}</li>)}</ul>
                  {entry.updates.map((update) => (
                    <div key={`${update.title} ${update.when}`} className="rail42-whatsnew-update">
                      <h4 className="rail42-whatsnew-heading">
                        <span className="rail42-whatsnew-update-title">{update.title}</span>
                        <span className="rail42-whatsnew-when">{update.when}</span>
                      </h4>
                      <ul className="rail42-whatsnew-lines">{update.lines.map((line) => <li key={line}>{line}</li>)}</ul>
                    </div>
                  ))}
                </li>
              );
            })}
          </ol>
        </div>
      </dialog>
    </div>
  );
}
