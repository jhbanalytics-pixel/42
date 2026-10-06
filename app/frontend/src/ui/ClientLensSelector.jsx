import React from 'react';

/* The Console's client lens entry. General 42 is the default and the first
   entry, so the surface always shows which configuration the question will run
   under. The roster is the server's authorized list for this scope: an entry
   the server did not authorize, a malformed one, and an unreadable roster all
   leave the general default alone rather than offering a lens that does not
   exist. Choosing a lens changes what the request is bound to, so the chosen
   entry names the configuration digest it carries. */

/* Quiet register, 23 Sept 2026: the select reads in the reader's words
   (rule 16). "Lens / General 42" named the product's own configuration; the
   label now says what is chosen, a client lens, and the default reads as the
   absence of one. */
export const GENERAL_LENS_LABEL = 'None';
const ROSTER_VERSION = 'client_lens_roster_v1';
const DIGEST = /^[0-9a-f]{64}$/;

const generalEntry = {clientLensId: '', label: GENERAL_LENS_LABEL, configurationDigest: null};

export function clientLensRoster(value){
  const rows = value && typeof value === 'object' && value.contract_version === ROSTER_VERSION
    && Array.isArray(value.lenses) ? value.lenses : [];
  const admitted = rows
    .filter((row) => row && typeof row === 'object'
      && typeof row.client_lens_id === 'string' && row.client_lens_id
      && typeof row.label === 'string' && row.label.trim()
      && typeof row.configuration_digest === 'string' && DIGEST.test(row.configuration_digest))
    .map((row) => ({
      clientLensId: row.client_lens_id,
      label: row.label,
      configurationDigest: row.configuration_digest,
    }));
  return [generalEntry, ...admitted];
}

export function selectedClientLens(roster, value){
  const entries = clientLensRoster(roster);
  return entries.find((entry) => entry.clientLensId === String(value ?? '')) || generalEntry;
}

/* What a named lens resolves to before a question may be asked under it. A name
   that is not yet decidable is never quietly answered as general 42: until the
   server's roster has arrived, a deep link naming a lens is unresolved, and a
   name the roster does not carry is unauthorized. Both states refuse the ask
   rather than running the question under the wrong configuration. */
export function resolveClientLensState(rosterState, roster, value){
  const requested = String(value ?? '');
  if (!requested) return {state: 'ready', lens: generalEntry, requested};
  if (rosterState !== 'ready') {
    return {state: rosterState === 'error' ? 'unavailable' : 'pending', lens: null, requested};
  }
  const found = clientLensRoster(roster).find((entry) => entry.clientLensId === requested);
  if (!found) return {state: 'unauthorized', lens: null, requested};
  return {state: 'ready', lens: found, requested};
}

export const LENS_STATE_MESSAGE = {
  pending: 'Reading the client lenses this scope authorizes. The question is not sent yet.',
  unavailable: 'The client lenses this scope authorizes could not be read, so a question cannot be asked under the lens this link names.',
  unauthorized: 'This scope does not authorize the client lens this link names, so no question is asked under it.',
};

export function ClientLensSelector({roster, value, onChange}){
  const entries = clientLensRoster(roster);
  const chosen = selectedClientLens(roster, value);
  return (
    <span className="workbench-lens" data-client-lens={chosen.clientLensId || 'general'}>
      <label className="workbench-lens-label" htmlFor="workbench-client-lens">Client lens</label>
      <select
        id="workbench-client-lens"
        className="workbench-lens-select"
        value={chosen.clientLensId}
        onChange={(event) => onChange(event.target.value)}
      >
        {entries.map((entry) => (
          <option key={entry.clientLensId || 'general'} value={entry.clientLensId}>{entry.label}</option>
        ))}
      </select>
      {chosen.configurationDigest
        ? <span className="workbench-lens-digest">Configuration {chosen.configurationDigest.slice(0, 12)}</span>
        : null}
    </span>
  );
}
