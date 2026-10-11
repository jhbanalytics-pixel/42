/* The version line (11 October 2026): one quiet line at the foot of the
   menu, "42 v2.3.0" with the short build sha beside it, so the team can say
   which release they are on. It opens a small What's new panel read from
   CHANGELOG.md, which the build bundles with VERSION and the sha
   (vite.config.mjs, __F42_RELEASE__).

   The page is built with the sha f42-api reports as its version, so when the
   server later answers with another sha a newer release has gone live under
   an open page, and a quiet hint offers a reload. The built sha is the
   expectation and the served one only an observation; a served value that is
   not a sha (a local "dev") is never a difference. App reads health once as
   the page loads, which is when the two agree by construction, so this line
   checks only when the reader comes back to the tab and on a slow interval,
   never more often than the gap. */
import {useCallback, useEffect, useId, useMemo, useRef, useState} from 'react';

const CHECK_GAP_MS = 5 * 60 * 1000;
const CHECK_EVERY_MS = 15 * 60 * 1000;
const SHA = /^[0-9a-f]{7,40}$/;
const HEADING = /^## (\d+\.\d+\.\d+) \((.+)\)$/;

export function builtRelease(){
  return typeof __F42_RELEASE__ === 'undefined' ? null : __F42_RELEASE__;
}

/* A heading "## 2.2.0 (11 October 2026)", one line naming the release, then
   one "- " line per change. Anything before the first heading is the
   numbering rule and is not shown. */
export function parseChangelog(text){
  const releases = [];
  for (const raw of String(text || '').split(/\r?\n/)){
    const line = raw.trim();
    const heading = line.match(HEADING);
    if (heading){
      const when = heading[2].charAt(0).toUpperCase() + heading[2].slice(1);
      releases.push({version: heading[1], when, next: /^next\b/i.test(heading[2]), summary: '', lines: []});
      continue;
    }
    const current = releases[releases.length - 1];
    if (!current || !line) continue;
    if (line.startsWith('- ')) current.lines.push(line.slice(2).trim());
    else if (!current.summary) current.summary = line;
  }
  return releases;
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

export function VersionNote({release = builtRelease(), active = true, gapMs = CHECK_GAP_MS, everyMs = CHECK_EVERY_MS, onReload = reloadPage}){
  if (!release || !release.version) return null;
  return <VersionLine release={release} active={active} gapMs={gapMs} everyMs={everyMs} onReload={onReload} />;
}

function VersionLine({release, active, gapMs, everyMs, onReload}){
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
        {short ? <><span className="sr-only">, build </span><span className="rail42-version-sha">{short}</span></> : null}
        <span className="sr-only">. What's new</span>
      </button>
      <div className="rail42-version-status" role="status">
        {stale ? <button type="button" className="rail42-version-reload" onClick={() => onReload()}>New version live, reload</button> : null}
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
            {releases.map((entry) => (
              <li key={entry.version} className="rail42-whatsnew-release" data-next={entry.next ? '' : undefined}>
                <h3 className="rail42-whatsnew-heading">
                  <span className="rail42-whatsnew-version">{entry.version}</span>
                  <span className="rail42-whatsnew-when">{entry.when}</span>
                </h3>
                {entry.summary ? <p className="rail42-whatsnew-summary">{entry.summary}</p> : null}
                <ul className="rail42-whatsnew-lines">{entry.lines.map((line) => <li key={line}>{line}</li>)}</ul>
              </li>
            ))}
          </ol>
        </div>
      </dialog>
    </div>
  );
}
