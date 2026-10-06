import {describe, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {exploreDynamicProps, topicOpenPath, topicRouteReader} from '../../App.jsx';
import {ReleasedSignalStory, isReleasedSignalId, releasedSignalRead} from '../../releasedSignal.jsx';
import {TodayPage} from '../../today.jsx';
import {ExplorePage} from '../../explore.jsx';
import {DESK_STATES, GREEN, RUN, signal} from '../../../tests/browser/fixtures/42-capability-routes/_desk.js';

/* U01: the Briefing lead's action and Discover's "Open topic story" open
   #/topic/<released signal id>. The topic API knows curated topics only, so
   a released signal id must be read from the same released run the Briefing
   and Discover render (exploreDynamicProps), never from /api/topic. */

const released = () => exploreDynamicProps(DESK_STATES.populated()).topics;
const LEAD = released()[0].signal;
const SECOND = released()[1].signal;

function hrefOf(markup, label){
  const match = new RegExp('href="([^"]+)"[^>]*>' + label + '<').exec(markup);
  return match ? match[1].replace(/&amp;/g, '&') : null;
}

describe('the topic route reads a released signal id from the released run', () => {
  test('a released signal id is recognised and routed off the topic API', () => {
    expect(isReleasedSignalId(LEAD.signal_id)).toBe(true);
    expect(isReleasedSignalId('food_braai')).toBe(false);
    expect(isReleasedSignalId('sig_' + 'a'.repeat(63))).toBe(false);
    expect(topicRouteReader({view: 'topic', param: LEAD.signal_id, error: null})).toBe('released_signal');
    expect(topicRouteReader({view: 'topic', param: 'food_braai', error: null})).toBe('topic');
    /* A malformed market selection keeps the topic route's own invalid link frame. */
    expect(topicRouteReader({view: 'topic', param: LEAD.signal_id, error: 'topic_scope_invalid'})).toBe('topic');
  });

  test('the Briefing lead action and the Discover link both name a signal the reader finds', () => {
    const briefing = renderToStaticMarkup(<TodayPage topics={released()} freshness={GREEN} deskDate={RUN.signal_date} />);
    const discover = renderToStaticMarkup(<ExplorePage topics={released()} freshness={GREEN} region="ZA" />);
    const fromBriefing = decodeURIComponent(hrefOf(briefing, 'Open ' + LEAD.signal_name).replace(/^#\/topic\//, '').split('?')[0]);
    const fromDiscover = decodeURIComponent(hrefOf(discover, 'Open topic story').replace(/^#\/topic\//, '').split('?')[0]);
    expect(releasedSignalRead({signalId: fromBriefing, topics: released()}).state).toBe('ready');
    expect(releasedSignalRead({signalId: fromDiscover, topics: released()}).state).toBe('ready');
  });

  test('a released signal page shows its actual lead, run date, market, window and sources', () => {
    const markup = renderToStaticMarkup(<ReleasedSignalStory signalId={LEAD.signal_id} topics={released()} />);
    expect(markup).toContain('<h1');
    expect(markup).toContain('>' + LEAD.signal_name + '</h1>');
    expect(markup).toContain('data-run-date="' + RUN.signal_date + '"');
    expect(markup).toContain('data-signal-id="' + LEAD.signal_id + '"');
    expect(markup).toContain('Run 12 Sept 2026');
    expect(markup).toContain('South Africa');
    expect(markup).toContain('18 to 24 Aug 2026');
    expect(markup).toContain(LEAD.why_now);
    expect(markup).toContain(LEAD.possible_response);
    for (const receipt of LEAD.receipts){
      expect(markup).toContain(receipt.excerpt);
      expect(markup).toContain(receipt.author_label);
      expect(markup).toContain('href="' + receipt.url + '"');
    }
    expect(markup).not.toContain('could not load');
    /* Another signal of the same run opens on its own identity. */
    const second = renderToStaticMarkup(<ReleasedSignalStory signalId={SECOND.signal_id} topics={released()} />);
    expect(second).toContain('>' + SECOND.signal_name + '</h1>');
    expect(second).not.toContain(LEAD.receipts[0].excerpt);
  });

  test('a field the run did not send is left out, never filled', () => {
    const row = signal(1, 'populated', {possible_response: null});
    const markup = renderToStaticMarkup(<ReleasedSignalStory signalId={row.signal.signal_id} topics={[row]} />);
    expect(markup).not.toContain('Possible response');
    expect(markup).toContain(row.signal.why_now);
  });

  test('a signal absent from the released run reads as plainly unavailable, with a way on', () => {
    const absent = 'sig_' + 'f'.repeat(64);
    const read = releasedSignalRead({signalId: absent, topics: released()});
    expect(read.state).toBe('unavailable');
    const markup = renderToStaticMarkup(<ReleasedSignalStory signalId={absent} topics={released()} />);
    expect(markup).toContain('<h1');
    expect(markup).toContain('This signal is not in the released run');
    expect(markup).toContain('href="#/explore"');
    expect(markup).toContain('href="#/pulse"');
    expect(markup).not.toContain('could not load');
    expect(markup).not.toContain('No signal for this topic');
  });

  test('a completed run with no discovery also reads the signal as unavailable', () => {
    const topics = exploreDynamicProps(DESK_STATES.empty()).topics;
    expect(releasedSignalRead({signalId: LEAD.signal_id, topics}).state).toBe('unavailable');
  });

  test('a held evidence authority is held here as it is on the Briefing', () => {
    const row = signal(1, 'held');
    const markup = renderToStaticMarkup(<ReleasedSignalStory signalId={row.signal.signal_id} topics={[row]} />);
    expect(markup).toContain('Held until the evidence authority is checked');
    expect(markup).not.toContain(row.signal.receipts[0].excerpt);
  });

  test('loading waits and a failed desk read says so with a retry', () => {
    const loading = renderToStaticMarkup(<ReleasedSignalStory signalId={LEAD.signal_id} topics={[]} loading />);
    expect(loading).toContain('data-released-signal-state="loading"');
    expect(loading).not.toContain('This signal is not in the released run');
    const failed = renderToStaticMarkup(<ReleasedSignalStory signalId={LEAD.signal_id} topics={[]} error={{code: 'desk_read_failed', message: 'The completed run store did not answer.'}} />);
    expect(failed).toContain('data-released-signal-state="error"');
    expect(failed).toContain('The released run could not load');
    expect(failed).toContain('Try again');
    expect(failed).not.toContain('This signal is not in the released run');
  });
});

describe('every Briefing action that carries a released signal opens its story', () => {
  test('a released run row handed to the Briefing open handler opens its signal id', () => {
    /* The Briefing's comparison rows pass the released row itself, which
       carries its id under signal.signal_id, not at the top level. */
    const row = released()[1];
    expect(row.id).toBeUndefined();
    expect(topicOpenPath(row)).toBe('/topic/' + SECOND.signal_id);
    expect(topicOpenPath({id: 'food_braai'})).toBe('/topic/food_braai');
    expect(topicOpenPath({generated: true, id: 'x'})).toBeNull();
    expect(topicOpenPath(null)).toBeNull();
    expect(topicOpenPath({signal: {signal_id: 'not a released id'}})).toBeNull();
  });
});
