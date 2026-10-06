/* A failure frame prints the code the read recorded: the producer's own where
   its body carried one, the status where it did not, and unstated where the
   caller handed over neither. A page derived finding carries its own name and
   is announced as derived. No frame prints a code nobody recorded. */
import {expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {buildExploreState} from '../../explore.jsx';
import {EvidenceRoomView, resolveEvidenceRoomView} from '../../evidenceRoom.jsx';
import {buildFieldworkState} from '../../fieldwork.jsx';
import {routeStateView, routeRunFacts} from '../../instrumentRouteModels.js';

const DERIVED = 'No producer said this; this page derived it from the records it read:';
const STATUS = 'The producer sent no reason; the read failed with status:';
const status = {contract_version: 'intelligence_dossier_v1', investigation_id: 'inv_1', research_plan: {questions: []}, missing_work: []};

test('Discover carries the producer code, else the status, else unstated', () => {
  expect(buildExploreState({error: {code: 'desk_read_failed'}}).code).toBe('desk_read_failed');
  expect(buildExploreState({error: {status: 503, message: 'Service unavailable right now.'}}).code).toBe('http_503');
  expect(buildExploreState({error: {message: 'the desk is offline right now.'}}).code).toBe('unstated');
  expect(buildExploreState({error: {code: '  '}}).code).toBe('unstated');
});

test('the route frame reason for a caller that hands over no code is unstated', () => {
  expect(routeStateView({state: 'error', message: 'offline'}, routeRunFacts({})).reason).toBe('unstated');
  expect(routeStateView({state: 'error', code: 'http_500'}, routeRunFacts({})).reason).toBe('http_500');
});

test('a claim resource without its list is a derived finding with its own name', () => {
  for (const broken of [null, {}, {claims: null}, {claims: 'none'}]){
    const view = resolveEvidenceRoomView(status, broken, null, null);
    expect(view.state).toBe('error');
    expect(view.error).toEqual({code: 'claim_list_missing', origin: 'derived'});
  }
});

test('the evidence room error frame says whose code it prints', () => {
  const derived = renderToStaticMarkup(<EvidenceRoomView state="error" error={{code: 'claim_list_missing', origin: 'derived'}} />);
  expect(derived).toContain(DERIVED + ' <code>claim_list_missing</code>');
  const failed = renderToStaticMarkup(<EvidenceRoomView state="error" error={{code: 'http_500'}} />);
  expect(failed).toContain(STATUS + ' <code>http_500</code>');
  const artifact = renderToStaticMarkup(<EvidenceRoomView state="empty" status={status} claims={[]} artifactError={{code: 'http_502'}} />);
  expect(artifact).toContain(STATUS + ' <code>http_502</code>');
});

/* Restated 2 October 2026: the retired Fieldwork printed failure codes and
   their origin. 42's Fieldwork reads the 42 API, whose 401 is the passcode
   screen and whose other failures carry a message; it shows the message and
   invents no code. */
test('fieldwork keeps the failure it was given and invents no code', () => {
  expect(buildFieldworkState({error: {code: 'authentication_required', status: 401, auth: true}})).toEqual({state: 'auth'});
  const failed = buildFieldworkState({error: {code: 'http_500', status: 500, message: 'The request failed (500).'}});
  expect(failed).toEqual({state: 'error', message: 'The request failed (500).'});
  expect(buildFieldworkState({error: {}})).toEqual({state: 'error', message: 'The 42 service did not answer.'});
});

test('the evidence room frame invents no code for a failure that recorded none', () => {
  const UNSTATED = 'The producer sent no reason:';
  for (const view of [
    <EvidenceRoomView state="error" error={{}} />,
    <EvidenceRoomView state="error" error={{code: ''}} />,
    <EvidenceRoomView state="empty" status={status} claims={[]} artifactError={{}} />,
  ]){
    const markup = renderToStaticMarkup(view);
    expect(markup).toContain(UNSTATED + ' <code>unstated</code>');
    expect(markup).not.toContain('workspace_unavailable');
    expect(markup).not.toContain('Reason the producer gave:');
  }
});
