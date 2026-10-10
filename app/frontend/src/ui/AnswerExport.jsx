/* Download a completed Ask answer with its cited sources.

   The request reference is the only thing sent. The server rebuilds the copy
   from the stored answer for this workspace and makes the PDF with its
   reviewed renderer, so a download can never carry anything the store did not
   record. A refusal comes back with plain words, which are shown as they are.
   The two downloads sit in the answer's one actions row; the row itself says
   what they are, so no hint line repeats it above them. */
import {useState} from 'react';
import {PASS_KEY, storedValue} from '../api.js';
import {releaseExportUrlLater} from '../exportUrl.js';
import {PEOPLE_UNAVAILABLE_WORDS} from '../privacyNotice.js';

const FORMATS = [
  {ext: 'html', label: 'Download as HTML'},
  {ext: 'pdf', label: 'Download as PDF'},
];
const FAILED = 'The download did not complete. Try again in a moment.';

async function refusalMessage(response){
  if (response.status === 401) return 'Sign in again to download this answer.';
  try {
    const body = await response.json();
    if (body && body.error === 'people_unavailable') return PEOPLE_UNAVAILABLE_WORDS;
    const message = body && body.detail && typeof body.detail === 'object' ? body.detail.message : null;
    if (typeof message === 'string' && message.trim()) return message;
  } catch (_error) { /* no readable reason */ }
  return FAILED;
}

function save(blob, name){
  const url = URL.createObjectURL(blob);
  try {
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    link.rel = 'noopener';
    document.body.appendChild(link);
    link.click();
    link.remove();
  } catch (error){
    URL.revokeObjectURL(url);
    throw error;
  }
  releaseExportUrlLater(url);
}

export function AnswerExport({requestId}){
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const download = async (ext) => {
    setBusy(ext);
    setError('');
    try {
      const response = await globalThis.fetch('/api/chat/answer/' + encodeURIComponent(requestId) + '/export.' + ext, {
        headers: {'X-Passcode': storedValue(PASS_KEY)},
      });
      if (!response.ok){ setError(await refusalMessage(response)); return; }
      save(await response.blob(), '42-answer-' + requestId.slice(0, 8) + '.' + ext);
    } catch (_error){
      setError(FAILED);
    } finally {
      setBusy('');
    }
  };
  return (
    <section className="general-export" aria-label="Download this answer">
      <div className="general-export-actions">
        {FORMATS.map(({ext, label}) => (
          <button type="button" className="general-more" key={ext} disabled={Boolean(busy)} aria-busy={busy === ext} onClick={() => download(ext)}>{label}</button>
        ))}
      </div>
      {error && <p className="general-note" role="alert">{error}</p>}
    </section>
  );
}
