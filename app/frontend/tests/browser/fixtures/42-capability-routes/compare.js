import {DESK_STATES} from './_desk.js';

export default {
  capability_id: 'compare',
  route: '#/compare',
  fixtures: (state) => ({'/api/desk': DESK_STATES[state]()}),
  gaps: {},
};
