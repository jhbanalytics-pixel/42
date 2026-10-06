/* Fan-out: one generate call per approved behaviour with scoped focus. */
import {
  focusPayloadForBehaviour,
  runGenerateBatch,
} from '../frontend/src/researchBatchCore.js';

const behaviours = [
  {id: 'za-rugby', market: 'za', query_group: 'sport_rugby', signal_topic: 'Rugby', behaviour: 'Springboks conversation'},
  {id: 'za-genz', market: 'za', query_group: 'genz_lifestyle', signal_topic: 'Gen Z lifestyle', behaviour: 'Gen Z street culture'},
  {id: 'ng-ankara', market: 'ng', query_group: 'fashion_ankara_asoebi', signal_topic: 'Ankara', behaviour: 'Asoebi season peaks'},
  {id: 'ng-japa', market: 'ng', query_group: 'culture_japa', signal_topic: 'Japa', behaviour: 'Japa talk rising'},
  {id: 'ke-sheng', market: 'ke', query_group: 'culture_sheng', signal_topic: 'Sheng', behaviour: 'Sheng slang spreads'},
  {id: 'ke-maandamano', market: 'ke', query_group: 'politics_maandamano', signal_topic: 'Maandamano', behaviour: 'Protest mobilisation'},
];

let failed = 0;

const zaFocus = focusPayloadForBehaviour(behaviours[0]);
if (zaFocus.queryGroups.join() !== 'sport_rugby' || zaFocus.markets.join() !== 'za') {
  console.error('FAIL focusPayloadForBehaviour market scope', zaFocus);
  failed++;
}

const calls = [];
const fakeGenerate = async ({personaId, markets, productFrame, focus, parentArtifactId}) => {
  calls.push({personaId, markets: markets.slice(), productFrame, focus, parentArtifactId});
  return {status: 'completed', artifact_id: 'ra_' + calls.length};
};

(async () => {
  const batch = await runGenerateBatch(
    'digital_architect_genz',
    ['za', 'ng', 'ke'],
    'Google AI Mode',
    behaviours,
    fakeGenerate,
    null,
  );

  if (batch.results.length !== behaviours.length) {
    console.error('FAIL batch count', batch.results.length, 'expected', behaviours.length);
    failed++;
  }

  if (calls.length !== behaviours.length) {
    console.error('FAIL generate call count', calls.length);
    failed++;
  }

  for (let i = 0; i < behaviours.length; i++){
    const b = behaviours[i];
    const call = calls[i];
    const focus = focusPayloadForBehaviour(b);
    if (!call) {
      console.error('FAIL missing call for index', i);
      failed++;
      continue;
    }
    if (call.markets.join() !== focus.markets.join()) {
      console.error('FAIL markets for', b.id, call.markets, focus.markets);
      failed++;
    }
    if (call.focus.queryGroups.join() !== focus.queryGroups.join()) {
      console.error('FAIL query_groups for', b.id, call.focus.queryGroups, focus.queryGroups);
      failed++;
    }
    if (call.focus.note !== focus.note) {
      console.error('FAIL focus note for', b.id, call.focus.note, focus.note);
      failed++;
    }
    if (i > 0 && call.parentArtifactId !== 'ra_1') {
      console.error('FAIL parent chain at', i, call.parentArtifactId);
      failed++;
    }
  }

  if (batch.parentArtifactId !== 'ra_1') {
    console.error('FAIL parentArtifactId', batch.parentArtifactId);
    failed++;
  }

  if (failed){
    console.error(`FAIL: ${failed} research batch fan-out check(s)`);
    process.exit(1);
  }
  console.log(`OK: ${behaviours.length} research batch fan-out checks passed`);
})().catch((err) => {
  console.error('FAIL research batch fan-out:', err);
  process.exit(1);
});
