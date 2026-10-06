import {expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {SourceSnapshot, sourceSnapshotModel} from '../SourceSnapshot.jsx';

function snapshot(){
  return {
    contract_version: '2.2.0', resource_version: 'source_lab_inventory_v2',
    catalog: {catalog_digest: 'c'.repeat(64), checked_at: '2026-09-06T00:16:15Z', completeness_state: 'verified_snapshot', route_count: 3},
    sources: [
      {endpoint_id: 'reels', platform: 'instagram', official_capability: 'Search Instagram reels', status: 'active', rows: 87, yield_state: 'measured', market: null, official_credits: 1},
      {endpoint_id: 'comments', platform: 'youtube', official_capability: 'List video comments', status: 'active', rows: 347, yield_state: 'measured', market: null, official_credits: 1},
      {endpoint_id: 'catalogue', platform: 'tiktok', official_capability: 'Documented search', status: 'inventory_only', rows: null, yield_state: 'unmeasured', official_credits: 1},
    ],
  };
}

test('the producer snapshot exposes per-route observations without summing overlapping rows', () => {
  const model = sourceSnapshotModel(snapshot());
  expect(model).toMatchObject({recordedAt: '2026-09-06T00:16:15Z', documented: 3, active: 2});
  expect(model.readings.map((row) => row.observations)).toEqual([87, 347]);
  expect(model).not.toHaveProperty('totalObservations');
  const html = renderToStaticMarkup(<SourceSnapshot payload={snapshot()} />);
  expect(html).toContain('Recorded observations');
  expect(html).toContain('87');
  expect(html).toContain('347');
  expect(html).not.toContain('434');
  expect(html).not.toContain('rows/read');
  expect(html).toContain('Observation window was not supplied.');
  expect(html).toContain('Catalogue prices do not measure spend.');
});

test('a measured zero remains zero while a missing reading stays absent', () => {
  const payload = snapshot();
  payload.sources[0].rows = 0;
  payload.sources[1].rows = null;
  expect(sourceSnapshotModel(payload).readings.map((row) => row.observations)).toEqual([0]);
});

test.each([NaN, Infinity, -1, '87', 1.5])('an invalid observation count %s is not displayed', (value) => {
  const payload = snapshot();
  payload.sources[0].rows = value;
  expect(sourceSnapshotModel(payload).readings.map((row) => row.id)).not.toContain('reels');
});

test('unmeasured numeric rows cannot become measured observations', () => {
  const payload = snapshot();
  payload.sources[0].yield_state = 'unmeasured';
  expect(sourceSnapshotModel(payload).readings.map((row) => row.id)).toEqual(['comments']);
});

test.each([
  (value) => { value.catalog.route_count = 4; },
  (value) => { value.sources.push(value.sources[0]); value.catalog.route_count += 1; },
  (value) => { value.catalog.checked_at = 'not-a-date'; },
  (value) => { value.catalog.catalog_digest = ''; },
  (value) => { value.catalog.completeness_state = 'pending'; },
  (value) => { value.contract_version = '1.0.0'; },
  (value) => { value.sources[0] = null; },
])('an incomplete or invalid snapshot cannot carry an activity claim', (mutate) => {
  const payload = snapshot();
  mutate(payload);
  expect(sourceSnapshotModel(payload)).toBeNull();
  expect(renderToStaticMarkup(<SourceSnapshot payload={payload} />)).toBe('');
});

test('a complete catalogue with no measured rows states the gap', () => {
  const payload = snapshot();
  payload.sources.forEach((row) => { row.rows = null; });
  const html = renderToStaticMarkup(<SourceSnapshot payload={payload} />);
  expect(html).toContain('This snapshot contains no measured observation counts.');
  expect(html).not.toContain('0 observations');
});
