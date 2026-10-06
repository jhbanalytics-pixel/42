export default {
  capability_id: 'history',
  route: '#/historical/inv_fixture/analogue',
  fixtures: () => ({'/api/internal/v2/investigations/inv_fixture/historical/read': {__status: 501, body: {detail: {code: 'historical_backend_gate_open', message: 'The historical backend gate has not passed.'}}}}),
  gaps: {},
};
