import {DESK_STATES} from './_desk.js';

export default {
  capability_id: 'discovery_exploration',
  route: '#/explore',
  fixtures: (state) => ({'/api/desk': DESK_STATES[state]()}),
  gaps: {},
  /* The job names two deep links beside #/explore. Neither is driven yet, so
     each is declared here and counted as an uncovered route of this row
     rather than passing as covered because no entry names it. */
  entry_gaps: {
    topic: 'The topic deep link the job names opens #/topic on its own read of src/api/desk.py; no entry of this capability drives that route yet, so the topic route is uncovered and stated here.',
    seedpath: 'The seed explorer deep link the job names opens #/seedpath on its own producer read; no entry of this capability drives that route yet, so the seedpath route is uncovered and stated here.',
  },
};
