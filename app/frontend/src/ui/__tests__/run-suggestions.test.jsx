import {describe, expect, test} from 'bun:test';
import {runSuggestions} from '../../suggestions.js';

function signal(name, market){
  return {signal: {contract_version: 'desk_dynamic_signal_v2', signal_name: name, market}};
}

function desk(status, signals){
  return {dynamic_discovery: {contract_version: 'desk_dynamic_discovery_v1', status, signals, run: {run_id: 'r'}, error: null}};
}

describe('runSuggestions reads the completed run only', () => {
  test('a ready run yields its admitted signal names for the market, in run order', () => {
    const data = desk('ready', [signal('Neighbourhood running clubs', 'za'), signal('Load shedding cooking hacks', 'za'), signal('Danfo fare protests', 'ng')]);
    expect(runSuggestions(data, 'za')).toEqual(['Neighbourhood running clubs', 'Load shedding cooking hacks']);
    expect(runSuggestions(data, 'ng')).toEqual(['Danfo fare protests']);
    expect(runSuggestions(data, 'all')).toEqual(['Neighbourhood running clubs', 'Load shedding cooking hacks', 'Danfo fare protests']);
  });

  test('a run that admitted nothing suggests nothing, and so does an unavailable one', () => {
    expect(runSuggestions(desk('no_discovery', []), 'za')).toEqual([]);
    expect(runSuggestions(desk('unavailable', []), 'za')).toEqual([]);
    expect(runSuggestions({topics: [{label: 'Amapiano'}]}, 'za')).toEqual([]);
    expect(runSuggestions(null, 'za')).toEqual([]);
  });

  test('names are deduplicated, blank names dropped, and the row is capped', () => {
    const data = desk('ready', [signal('Same name', 'ke'), signal('same name', 'ke'), signal('   ', 'ke'), signal('Second', 'ke'), signal('Third', 'ke')]);
    expect(runSuggestions(data, 'ke', 2)).toEqual(['Same name', 'Second']);
  });

  test('a ready status without a signal list is not a completed run', () => {
    expect(runSuggestions(desk('ready', undefined), 'za')).toEqual([]);
  });
});
