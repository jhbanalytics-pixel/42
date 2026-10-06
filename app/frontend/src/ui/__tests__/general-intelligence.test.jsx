import {expect, test} from 'bun:test';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {GeneralIntelligence} from '../GeneralIntelligence.jsx';
import {validateIntelligenceReply, intelligenceHistoryText, sha256Hex} from '../../generalIntelligence.js';
import {intelligenceFixture} from './fixtures/general-intelligence.js';
import {storedReplyFixture} from './fixtures/stored-reply-60a5fd02.js';

test('empty successful retrieval explains insufficiency without claiming an outage', () => {
  const source = intelligenceFixture();
  Object.assign(source, {status: 'unavailable', snapshot_id: null, sections: [], claims: [], receipts: [], readings: []});
  source.missing_work = ['evidence_insufficient', 'no_matching_evidence'];
  const before = JSON.stringify(source);
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).toContain('No admissible evidence matched this request.');
  expect(html).toContain('The requested answer cannot be established from this read.');
  expect(html).not.toContain('retrieval did not complete');
  expect(html).not.toContain('evidence_insufficient');
  expect(html).not.toContain('no_matching_evidence');
  expect(JSON.stringify(source)).toBe(before);
});

test('generic insufficient evidence does not claim that retrieval matched nothing', () => {
  const source = intelligenceFixture();
  source.missing_work = ['evidence_insufficient'];
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).toContain('The available evidence cannot support this answer.');
  expect(html).not.toContain('No admissible evidence matched');
});

test('missing evidence codes render readable copy without mutating supplied response or history', () => {
  const source = intelligenceFixture();
  source.missing_work = ['retrieval_incomplete', 'coverage_incomplete', 'corroborated_foreign_local_only', 'parent_source_unavailable', 'answer_invalid', 'A future supplied explanation.', '__proto__', 'toString'];
  const before = JSON.stringify(source);
  const historyBefore = intelligenceHistoryText(source);
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).toContain('The evidence retrieval did not complete. No answer was generated from it.');
  expect(html).toContain('The available evidence does not cover the requested dates or scope.');
  expect(html).toContain('The matching records describe local events outside the requested market. Relevant evidence is still needed.');
  expect(html).toContain('A previously cited source could not be verified for this request.');
  expect(html).toContain('The draft answer did not pass evidence validation. No answer was published.');
  expect(html).toContain('A future supplied explanation.');
  expect(html).toContain('<p>__proto__</p>');
  expect(html).toContain('<p>toString</p>');
  expect(html).not.toContain('retrieval_incomplete');
  expect(html).not.toContain('corroborated_foreign_local_only');
  expect(html).not.toContain('parent_source_unavailable');
  expect(html).not.toContain('answer_invalid');
  expect(JSON.stringify(source)).toBe(before);
  expect(intelligenceHistoryText(source)).toBe(historyBefore);
});

test('known structured record metrics read clearly without changing evidence', () => {
  const source = intelligenceFixture();
  source.readings.push({reading_id: 'reading_1', value: 10, unit: 'records', window: {...source.window}, method: 'selected_receipt_count', denominator: null, source_receipt_ids: ['receipt_1'], limitations: ['Not population prevalence.']});
  source.claims[0].reading_ids = ['reading_1'];
  source.claims[0].text = `selected_receipt_count: 10 records (${source.window.start} to ${source.window.end}).`;
  source.receipts[0].platform = 'reddit';
  source.receipts[0].source_label = 'socialcrawl';
  source.receipts[0].url = null;
  const before = JSON.stringify(source);
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).toContain('Selected source records: 10 records');
  expect(html).not.toContain('selected_receipt_count');
  expect(html).toContain('Open R1: Reddit');
  expect(html).toContain('<summary>R1 · Reddit</summary>');
  expect(html).not.toContain('socialcrawl');
  expect(html).toContain('Source link: Not available in the released data');
  expect(html).toContain('Not population prevalence.');
  expect(html).toContain('aria-controls="source-');
  expect(JSON.stringify(source)).toBe(before);
});

test('unknown metrics and ordinary claim text retain their supplied content', () => {
  const source = intelligenceFixture();
  const original = source.claims[0].text;
  source.readings.push({reading_id: 'reading_1', value: null, unit: 'records', window: {...source.window}, method: 'future_metric', denominator: null, source_receipt_ids: ['receipt_1'], limitations: []});
  source.claims[0].reading_ids = ['reading_1'];
  source.receipts[0].platform = null;
  source.receipts[0].source_label = 'Supplied source';
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).toContain(original);
  expect(html).toContain('Method: future_metric');
  expect(html).toContain('<strong>Unmeasured</strong>');
  expect(html).toContain('Open R1: Supplied source');
  source.readings[0].method = 'selected_receipt_count';
  expect(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}))).toContain(original);
});

test.each([['google_search', 'Google Search'], ['twitter', 'X'], ['web_attention', 'Web attention'], ['app_store', 'App Store'], ['unknown_search_provider', 'unknown_search_provider']])('source controls label %s without inferring a different platform', (platform, label) => {
  const source = intelligenceFixture();
  source.receipts[0].platform = platform;
  source.receipts[0].source_label = 'socialcrawl';
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).toContain(`Open R1: ${label}`);
  expect(html).toContain(`<summary>R1 · ${label}</summary>`);
});

test.each([[1e-15, '0,000000000000001'], [0.00001, '0,00001'], [0, '0']])('keeps the supplied nonzero reading and denominator %s', (number, expected) => {
  const source = intelligenceFixture();
  source.readings.push({reading_id: 'reading_1', value: number, unit: 'units', window: {...source.window}, method: 'Supplied measurement', denominator: number, source_receipt_ids: ['receipt_1'], limitations: []});
  source.claims[0].reading_ids = ['reading_1'];
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).toContain(`<strong>${expected}</strong>`);
  expect(html).toContain(`Denominator: ${expected}</p>`);
});

test('a bounded partial reply retains its actual scope and evidence', () => {
  const source = intelligenceFixture();
  const result = validateIntelligenceReply(source);
  expect(result.ok).toBe(true);
  expect(result.value.resolved_scope.audience_lens_ids).toEqual([]);
  expect(result.receipts.get('receipt_1').published_at).toBe('2026-09-03T10:00:00Z');
});

test.each([
  ['unknown receipt', value => { value.claims[0].receipt_ids = ['unknown']; }],
  ['cross snapshot', value => { value.receipts[0].snapshot_id = 'another'; }],
  ['cross market', value => { value.receipts[0].market = 'ng'; }],
  ['duplicate receipt', value => { value.receipts.push({...value.receipts[0]}); }],
  ['cycle', value => { value.claims[0].parent_claim_ids = ['claim_1']; }],
  ['unknown version', value => { value.contract_version = 'future_version'; }],
  ['missing window', value => { value.window = null; }],
  ['missing snapshot', value => { value.snapshot_id = null; }],
  ['uncited assertion', value => { value.claims[0].receipt_ids = []; }],
  ['hidden extra field', value => { value.claims[0].model_memory = 'hidden'; }],
  ['invalid date', value => { value.receipts[0].published_at = '2026-02-30T00:00:00Z'; }],
  ['future source', value => { value.receipts[0].collected_at = '2026-09-07T00:00:00Z'; }],
  ['invalid offset', value => { value.receipts[0].published_at = '2026-09-03T00:00:00+00:99'; }],
  ['normalized invalid hour', value => { value.receipts[0].published_at = '2026-09-03T24:00:00Z'; }],
])('rejects %s', (_name, mutate) => {
  const source = intelligenceFixture(); mutate(source);
  expect(validateIntelligenceReply(source).ok).toBe(false);
});

test('unresolved usage remains unknown on unavailable results', () => {
  const source = intelligenceFixture();
  Object.assign(source, {status: 'unavailable', window: null, snapshot_id: null, sections: [], claims: [], receipts: []});
  Object.assign(source.usage, {status: 'unresolved', input_tokens: null, output_tokens: null, reason: 'response_usage_unavailable'});
  expect(validateIntelligenceReply(source).ok).toBe(true);
  expect(intelligenceHistoryText(source)).toBeNull();
  source.status = 'complete';
  expect(validateIntelligenceReply(source).ok).toBe(false);
});

test('a proposal cannot acquire downstream authority by being outside sections', () => {
  const source = intelligenceFixture();
  source.ready_for_downstream = true;
  source.claims.push({...source.claims[0], claim_id: 'proposal_1', kind: 'proposal', support_state: 'proposed', falsifier: 'Evidence may contradict this action.'});
  expect(validateIntelligenceReply(source).ok).toBe(false);
});

test('resolved generation usage needs its receipts', () => {
  const source = intelligenceFixture();
  source.usage.usage_receipt_ids = [];
  source.usage.call_receipt_ids = [];
  expect(validateIntelligenceReply(source).ok).toBe(false);
});

test('a closed window cannot include an unfinished observation day', () => {
  const source = intelligenceFixture(); source.window.end = '2026-09-06';
  expect(validateIntelligenceReply(source).ok).toBe(false);
});

test.each(['unavailable', 'refused', 'needs_clarification'])('a %s reply cannot label a future window closed', status => {
  const source = intelligenceFixture();
  Object.assign(source, {status, clarification: 'Which period?', window: {start: '2030-01-01', end: '2030-01-02', closed: true}});
  expect(validateIntelligenceReply(source).ok).toBe(false);
});

test('history is built from admitted sections and excludes malformed replies', () => {
  const source = intelligenceFixture();
  expect(intelligenceHistoryText(source)).toContain(source.claims[0].text);
  source.receipts[0].snapshot_id = 'another';
  expect(intelligenceHistoryText(source)).toBeNull();
});

test('outer failure cannot render an inner completed answer', () => {
  const source = intelligenceFixture(); source.status = 'complete'; source.missing_work = [];
  expect(validateIntelligenceReply(source, {terminalError: true}).ok).toBe(false);
});

test('a closed reading window cannot lie after admission', () => {
  const source = intelligenceFixture();
  source.readings.push({reading_id: 'reading_1', value: 42, unit: 'records', window: {start: '2030-01-01', end: '2030-01-02', closed: true}, method: 'Count', denominator: null, source_receipt_ids: ['receipt_1'], limitations: []});
  source.claims[0].reading_ids = ['reading_1'];
  expect(validateIntelligenceReply(source).ok).toBe(false);
});

test('unresolved metering must identify its held attempt', () => {
  const source = intelligenceFixture();
  Object.assign(source, {status: 'unavailable', window: null, snapshot_id: null, sections: [], claims: [], receipts: []});
  Object.assign(source.usage, {status: 'unresolved', model_calls: null, input_tokens: null, output_tokens: null, call_receipt_ids: [], reservation_ids: [], reason: 'response_usage_unavailable'});
  expect(validateIntelligenceReply(source).ok).toBe(false);
});

test('history preserves proposal restrictions and falsifiers', () => {
  const source = intelligenceFixture(); source.review_required = true;
  source.claims.push({...source.claims[0], claim_id: 'proposal_1', kind: 'proposal', support_state: 'proposed', text: 'Launch this campaign.', limitations: ['Do not execute without review.'], falsifier: 'Stop if later evidence contradicts it.'});
  source.sections.push({kind: 'actions', claim_ids: ['proposal_1']});
  const history = intelligenceHistoryText(source);
  expect(history).toContain('Proposal: Launch this campaign.');
  expect(history).toContain('Do not execute without review.');
  expect(history).toContain('Stop if later evidence contradicts it.');
  expect(history).toContain('Review required');
});

/* Receipts emitted since the challenge_v5 adapter carry evidence_purposes and
   quote_bindings beside the seventeen legacy keys. The span digest below is the
   SHA-256 of 'repair tutorial', pinned here rather than read from the receipt. */
const SPAN_DIGEST = '0ed65bbcc7cd45cf745769f582d2526358c451fe336246e014c39937eaab4a87';
function boundReceipt(receipt, bindings = true){
  return {...receipt,
    evidence_purposes: [{claim_id: 'claim_1', evidence_purpose: bindings ? 'support' : 'context'}],
    quote_bindings: bindings ? [{claim_id: 'claim_1', content_digest: receipt.content_digest, end: 17, quote_sha256: SPAN_DIGEST, source_field: 'excerpt', start: 2}] : [],
  };
}
function boundFixture(){
  const source = intelligenceFixture();
  source.receipts[0] = boundReceipt(source.receipts[0]);
  return source;
}

test('the stored reply for request 60a5fd02 validates and renders its cited answer', () => {
  const source = storedReplyFixture();
  expect(source.receipts.length).toBe(24);
  expect(source.receipts.every(receipt => Object.keys(receipt).length === 19)).toBe(true);
  const result = validateIntelligenceReply(source);
  expect(result).toMatchObject({ok: true});
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).not.toContain('Reply withheld');
  expect(html).toContain('data-intelligence-state="partial"');
  expect(html).toContain('<summary>R1 · ');
  expect(html).toContain('24 records in this reply.');
});

test('a seventeen key legacy receipt still validates and renders', () => {
  const source = intelligenceFixture();
  expect(Object.keys(source.receipts[0]).length).toBe(17);
  expect(validateIntelligenceReply(source)).toMatchObject({ok: true});
  expect(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}))).not.toContain('Reply withheld');
});

test('a nineteen key receipt validates and names no failing field', () => {
  const result = validateIntelligenceReply(boundFixture());
  expect(result.ok).toBe(true);
  expect(result).not.toHaveProperty('field');
});

test('an evidence purpose outside support, challenge and context is withheld by name', () => {
  const source = boundFixture();
  source.receipts[0].evidence_purposes[0].evidence_purpose = 'decoration';
  expect(validateIntelligenceReply(source)).toMatchObject({ok: false, field: 'evidence_purposes', reason: 'The structured reply is incomplete or inconsistent. Its answer has been withheld.'});
});

test('an evidence purpose naming an unknown claim is withheld by name', () => {
  const source = boundFixture();
  source.receipts[0].evidence_purposes[0].claim_id = 'claim_unknown';
  expect(validateIntelligenceReply(source)).toMatchObject({ok: false, field: 'evidence_purposes'});
});

test('a quote binding whose content digest differs from its receipt is withheld by name', () => {
  const source = boundFixture();
  source.receipts[0].quote_bindings[0].content_digest = 'c'.repeat(64);
  expect(validateIntelligenceReply(source)).toMatchObject({ok: false, field: 'quote_bindings'});
});

test.each([
  ['end equal to start', binding => { binding.end = binding.start; }],
  ['end before start', binding => { binding.end = 1; }],
  ['end past the excerpt', binding => { binding.end = 1000; }],
  ['negative start', binding => { binding.start = -1; }],
  ['a field other than excerpt', binding => { binding.source_field = 'body'; }],
  ['a malformed quote digest', binding => { binding.quote_sha256 = 'not a digest'; }],
  ['an unknown claim', binding => { binding.claim_id = 'claim_unknown'; }],
  ['a hidden extra field', binding => { binding.model_memory = 'hidden'; }],
])('a quote binding with %s is withheld by name', (_name, mutate) => {
  const source = boundFixture(); mutate(source.receipts[0].quote_bindings[0]);
  expect(validateIntelligenceReply(source)).toMatchObject({ok: false, field: 'quote_bindings'});
});

test.each(['evidence_purposes', 'quote_bindings'])('an eighteen key receipt carrying only %s is withheld as a receipt shape', key => {
  const source = boundFixture();
  delete source.receipts[0][key === 'evidence_purposes' ? 'quote_bindings' : 'evidence_purposes'];
  expect(Object.keys(source.receipts[0]).length).toBe(18);
  expect(validateIntelligenceReply(source)).toMatchObject({ok: false, field: 'receipt'});
});

test('the failure object keeps the user facing reason and names the legacy field', () => {
  const source = intelligenceFixture(); source.receipts[0].snapshot_id = 'another';
  expect(validateIntelligenceReply(source)).toMatchObject({ok: false, field: 'receipt_identity', reason: 'The structured reply is incomplete or inconsistent. Its answer has been withheld.'});
});

test('the source record shows each bound quoted span and nothing for an unbound receipt', () => {
  const source = boundFixture();
  source.receipts.push(boundReceipt({...source.receipts[0], receipt_id: 'receipt_2', citation_label: 'R2', source_row_id: 'post_2', url: 'https://example.test/post_2', excerpt: 'A second post with no bound span.', content_digest: 'd'.repeat(64)}, false));
  source.claims[0].receipt_ids = ['receipt_1', 'receipt_2'];
  const before = JSON.stringify(source);
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).toContain('<summary>R1 · Instagram</summary>');
  expect(html).toContain('<summary>R2 · Instagram</summary>');
  expect(html).toContain('<q>repair tutorial</q>');
  expect(html.match(/class="general-quote"/g)).toHaveLength(1);
  expect(html.indexOf('<q>repair tutorial</q>')).toBeGreaterThan(html.indexOf('<summary>R1 · Instagram</summary>'));
  expect(html.indexOf('<q>repair tutorial</q>')).toBeLessThan(html.indexOf('<summary>R2 · Instagram</summary>'));
  expect(JSON.stringify(source)).toBe(before);
});

test('the stored reply renders the quoted span of its first citation', () => {
  const source = storedReplyFixture();
  const first = source.receipts[0];
  const binding = first.quote_bindings[0];
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  const span = first.excerpt.slice(binding.start, binding.end);
  expect(span).toContain('&');
  expect(html).toContain(`<q>${span.replace(/&/g, '&amp;')}</q>`);
});

/* A quote binding is shown only when the browser can prove it. The span digest
   is recomputed as SHA-256 over the UTF-8 bytes of the excerpt between the
   code point offsets, exactly as the engine's quote_span computes it, and the
   bound claim must cite the receipt. Digests below are pinned from the engine
   computation, never read from the payload under test. */
function storedBound(){
  const source = storedReplyFixture();
  const receipt = source.receipts.find(item => item.quote_bindings.length);
  return {source, receipt, binding: receipt.quote_bindings[0]};
}
function withheldQuote(source){
  expect(validateIntelligenceReply(source)).toMatchObject({ok: false, field: 'quote_bindings'});
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).toContain('Reply withheld');
  expect(html).not.toContain('Cited span');
}

test('the stored 60a5fd02 binding recomputes to its engine digest', () => {
  const {source, receipt, binding} = storedBound();
  expect(binding.quote_sha256).toBe('4ba3622c5932a99f320b2239b6b9bd484c5fb59ebfc321def91b671bb8c3fb76');
  expect(receipt.excerpt.length).toBe(97);
  expect(validateIntelligenceReply(source)).toMatchObject({ok: true});
});

test('a forged quote digest over a moved span is withheld', () => {
  const {source, receipt, binding} = storedBound();
  binding.quote_sha256 = '0'.repeat(64); binding.start = 0; binding.end = Math.min(40, receipt.excerpt.length);
  withheldQuote(source);
});

test('a well formed digest that does not match the bound span is withheld', () => {
  const {source, binding} = storedBound();
  binding.quote_sha256 = SPAN_DIGEST;
  withheldQuote(source);
});

test('a span moved away from the text its digest was computed over is withheld', () => {
  const {source, binding} = storedBound();
  binding.start = 1;
  withheldQuote(source);
});

test('an excerpt altered under an unchanged content digest is withheld', () => {
  const source = boundFixture();
  source.receipts[0].excerpt = 'A rePair tutorial shared with the community.';
  withheldQuote(source);
});

test('a binding attributed to a claim that does not cite its receipt is withheld', () => {
  const {source, receipt, binding} = storedBound();
  const other = source.claims.find(claim => !claim.receipt_ids.includes(receipt.receipt_id));
  expect(other).toBeTruthy();
  binding.claim_id = other.claim_id;
  withheldQuote(source);
});

const EMOJI_EXCERPT = '\u{1F525} Kasi weekend news: Soweto derby tickets sold out';
function emojiFixture(binding){
  const source = intelligenceFixture();
  source.receipts[0] = {...source.receipts[0], excerpt: EMOJI_EXCERPT,
    evidence_purposes: [{claim_id: 'claim_1', evidence_purpose: 'support'}],
    quote_bindings: [{claim_id: 'claim_1', content_digest: source.receipts[0].content_digest, source_field: 'excerpt', ...binding}]};
  return source;
}

test('engine code point offsets after an emoji render the exact cited span', () => {
  const source = emojiFixture({start: 21, end: 33, quote_sha256: '5066a200d6b0377b6c00013f4fd5e8260b0d319c906846344e7ecdf22f52b4fa'});
  expect(validateIntelligenceReply(source)).toMatchObject({ok: true});
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).toContain('Cited span: <q>Soweto derby</q>');
});

test('a span that includes an emoji hashes its UTF-8 bytes and renders whole', () => {
  const source = emojiFixture({start: 0, end: 6, quote_sha256: 'c57b20d2bff070feae32c9f95791bc6d0ba3c4e29442ddfc82932405eabe0c25'});
  expect(validateIntelligenceReply(source)).toMatchObject({ok: true});
  expect(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}))).toContain('Cited span: <q>\u{1F525} Kasi</q>');
});

test('UTF-16 offsets for the same span are withheld', () => {
  withheldQuote(emojiFixture({start: 22, end: 34, quote_sha256: '5066a200d6b0377b6c00013f4fd5e8260b0d319c906846344e7ecdf22f52b4fa'}));
});

test('an end offset past the excerpt in code points is withheld', () => {
  const length = Array.from(EMOJI_EXCERPT).length;
  expect(EMOJI_EXCERPT.length).toBe(length + 1);
  /* SHA-256 of 'Soweto derby tickets sold out', the span 21 to the end, so the
     digest is right and only the code point end limit can withhold it. */
  const TAIL_DIGEST = '0d76547310ac6554990f1d1f27fd4c7d9ec9110768b21f9b67ad0154810638eb';
  expect(validateIntelligenceReply(emojiFixture({start: 21, end: length, quote_sha256: TAIL_DIGEST}))).toMatchObject({ok: true});
  withheldQuote(emojiFixture({start: 21, end: length + 1, quote_sha256: TAIL_DIGEST}));
});

test.each([
  ['', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'],
  ['abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'],
  ['abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq', '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1'],
])('the browser quote digest matches the published SHA-256 vector for %p', (input, expected) => {
  expect(sha256Hex(input)).toBe(expected);
});

test('an evidence purpose attributed to a claim that does not cite its receipt is withheld by name', () => {
  const source = storedReplyFixture();
  const receipt = source.receipts.find(item => item.evidence_purposes.length);
  const other = source.claims.find(claim => !claim.receipt_ids.includes(receipt.receipt_id));
  expect(other).toBeTruthy();
  receipt.evidence_purposes[0].claim_id = other.claim_id;
  expect(validateIntelligenceReply(source)).toMatchObject({ok: false, field: 'evidence_purposes'});
  expect(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}))).toContain('Reply withheld');
});

test('every stored 60a5fd02 evidence purpose names a claim citing its receipt', () => {
  const source = storedReplyFixture();
  const claims = new Map(source.claims.map(claim => [claim.claim_id, claim]));
  expect(source.receipts.every(receipt => receipt.evidence_purposes.every(item => claims.get(item.claim_id).receipt_ids.includes(receipt.receipt_id)))).toBe(true);
  expect(validateIntelligenceReply(source)).toMatchObject({ok: true});
});

/* A pattern test reads its argument as a string, so a one item list holding a
   valid value used to pass. The server applies the same checks to an export and
   takes a list for what it is. */
test.each([
  ['request reference', source => { source.request_id = [source.request_id]; }, 'request'],
  ['citation label', source => { source.receipts[0].citation_label = [source.receipts[0].citation_label]; }, 'citation_label'],
])('a %s held in a list is withheld by name', (_name, mutate, field) => {
  const source = intelligenceFixture(); mutate(source);
  expect(validateIntelligenceReply(source)).toMatchObject({ok: false, field});
});

/* The engine lists each unfulfilled mandatory requirement twice: first by its
   planned requirement id, carried from the snapshot, then as the readable
   "Missing required evidence" line built from the planned question, in the
   same plan order. The view shows the readable line and keeps the id only
   behind a requirement reference disclosure. */
/* Quiet register, 23 Sept 2026: the answer redesign sets every section head
   in the answer as an h3 under the page's own heading, so the still needed
   section is found by its h3; what the section holds is checked unchanged. */
function stillNeeded(html){
  const start = html.indexOf('<h3>What is still needed</h3>');
  expect(start).toBeGreaterThan(-1);
  return html.slice(start, html.indexOf('</section>', start));
}

test('the stored reply shows the planned question, not the requirement id, as still needed', () => {
  const source = storedReplyFixture();
  const before = JSON.stringify(source);
  const historyBefore = intelligenceHistoryText(source);
  const section = stillNeeded(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source})));
  const question = 'Missing required evidence: What topics, cultural events, and conversations are trending in South Africa during the observation window?';
  expect(section).not.toContain('<p>req_trending_topics_za</p>');
  expect(section).toContain(`<p>${question}</p>`);
  expect(section).toContain('<p>Challenge search incomplete: no challenge requirement was planned.</p>');
  expect(section).toContain('<summary>Requirement reference</summary><p class="general-reference">req_trending_topics_za</p>');
  expect(section.indexOf('req_trending_topics_za')).toBeGreaterThan(section.indexOf(question));
  expect(JSON.stringify(source)).toBe(before);
  expect(intelligenceHistoryText(source)).toBe(historyBefore);
  expect(source.missing_work[0]).toBe('req_trending_topics_za');
});

test('each requirement id pairs with its readable line in plan order around known codes', () => {
  const source = intelligenceFixture();
  source.missing_work = ['req_za_trending_topics_sep_2026', 'req_za_brand_mentions', 'parent_source_unavailable', 'Missing required evidence: What was trending?', 'Missing required evidence: Which brands were named?'];
  const section = stillNeeded(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source})));
  expect(section).not.toContain('<p>req_za_trending_topics_sep_2026</p>');
  expect(section).not.toContain('<p>req_za_brand_mentions</p>');
  expect(section).toContain('A previously cited source could not be verified for this request.');
  const first = section.indexOf('<p>Missing required evidence: What was trending?</p>');
  const second = section.indexOf('<p>Missing required evidence: Which brands were named?</p>');
  const firstId = section.indexOf('>req_za_trending_topics_sep_2026<');
  const secondId = section.indexOf('>req_za_brand_mentions<');
  expect(first).toBeGreaterThan(-1);
  expect(first).toBeLessThan(firstId);
  expect(firstId).toBeLessThan(second);
  expect(second).toBeLessThan(secondId);
});

test('an identifier without a readable counterpart still renders as supplied', () => {
  const source = intelligenceFixture();
  source.missing_work = ['req_orphan', 'req_second', 'Missing required evidence: Only one question?'];
  const section = stillNeeded(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source})));
  expect(section).toContain('<p>req_orphan</p>');
  expect(section).toContain('<p>req_second</p>');
  expect(section).toContain('<p>Missing required evidence: Only one question?</p>');
  expect(section).not.toContain('Requirement reference');
});

/* Pairing needs distinct readable lines and distinct ids, and drops a paired
   id only at its own position ahead of the first readable line, so nothing
   the reply carries leaves the view. */
test('repeated readable lines leave every requirement id rendered as supplied', () => {
  const source = intelligenceFixture();
  source.missing_work = ['req_a', 'req_b', 'Missing required evidence: Q', 'Missing required evidence: Q'];
  expect(validateIntelligenceReply(source)).toMatchObject({ok: true});
  const section = stillNeeded(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source})));
  expect(section).toContain('<p>req_a</p>');
  expect(section).toContain('<p>req_b</p>');
  expect(section.split('<p>Missing required evidence: Q</p>').length).toBe(3);
  expect(section).not.toContain('Requirement reference');
});

test('repeated requirement ids leave every entry rendered as supplied', () => {
  const source = intelligenceFixture();
  source.missing_work = ['req_a', 'req_a', 'Missing required evidence: Q1', 'Missing required evidence: Q2'];
  expect(validateIntelligenceReply(source)).toMatchObject({ok: true});
  const section = stillNeeded(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source})));
  expect(section.split('<p>req_a</p>').length).toBe(3);
  expect(section).toContain('<p>Missing required evidence: Q1</p>');
  expect(section).toContain('<p>Missing required evidence: Q2</p>');
  expect(section).not.toContain('Requirement reference');
});

test('a paired id repeated after the readable lines still renders there', () => {
  const source = intelligenceFixture();
  source.missing_work = ['req_a', 'Missing required evidence: Q', 'req_a'];
  expect(validateIntelligenceReply(source)).toMatchObject({ok: true});
  const section = stillNeeded(renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source})));
  const question = section.indexOf('<p>Missing required evidence: Q</p>');
  const reference = section.indexOf('<summary>Requirement reference</summary><p class="general-reference">req_a</p>');
  const trailing = section.indexOf('<p>req_a</p>');
  expect(question).toBeGreaterThan(-1);
  expect(reference).toBeGreaterThan(question);
  expect(trailing).toBeGreaterThan(reference);
});

/* An empty match is an answer in its own right: the evidence does not
   support one. It reads as that, in the reply's own status word, and never as
   the generic failure line or the unavailable status an outage carries. */
test('an insufficient evidence reply says plainly that there is not enough evidence', () => {
  const source = intelligenceFixture();
  Object.assign(source, {status: 'unavailable', snapshot_id: null, sections: [], claims: [], receipts: [], readings: [], limitations: ['No completed answer is available.'], missing_work: ['evidence_insufficient', 'no_matching_evidence'], review_required: false, ready_for_downstream: false});
  const html = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: source}));
  expect(html).toContain('Not enough evidence');
  expect(html).toContain('There is not enough evidence to answer this question. Nothing in the records for these dates supports an answer, so none was generated.');
  expect(html).toContain('No completed answer is available.');
  expect(html).toContain('data-status="unavailable"');
  expect(html).not.toContain('Reply unavailable');
  expect(html).not.toContain('could not be completed from the admitted evidence and execution results');
  const outage = intelligenceFixture();
  Object.assign(outage, {status: 'unavailable', snapshot_id: null, sections: [], claims: [], receipts: [], readings: [], limitations: ['No completed answer is available.'], missing_work: ['model_timeout'], review_required: false, ready_for_downstream: false});
  const failed = renderToStaticMarkup(createElement(GeneralIntelligence, {intelligence: outage}));
  expect(failed).toContain('Reply unavailable');
  expect(failed).toContain('This request could not be completed from the admitted evidence and execution results.');
  expect(failed).not.toContain('Not enough evidence');
});
