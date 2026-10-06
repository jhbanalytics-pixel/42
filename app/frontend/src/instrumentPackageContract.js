import manifest from 'ogilvy-intelligence-design-system/manifest.json';
import {safePlainData} from './privatePlainData.js';

const REQUIRED_EXPORTS = Object.freeze([
  'AudienceUnavailable',
  'Banner',
  'CitedAnswer',
  'ComparisonInstrument',
  'DiscoverInstrument',
  'EVIDENCE_STATES',
  'EditorialLead',
  'EmptyState',
  'EvidenceDrawer',
  'EvidenceLedger',
  'EvidenceReadiness',
  'EvidenceRoom',
  'FieldworkInstrument',
  'HeldNotice',
  'InstrumentBriefing',
  'InstrumentShell',
  'IntelligenceConsole',
  'MarketChip',
  'MeasurementBands',
  'MomentumPill',
  'OgilvyMasthead',
  'OgilvyRoot',
  'OgilvyShell',
  'PlatformGlyph',
  'ProofRibbon',
  'Skeleton',
  'SourceInventoryRow',
  'StateView',
  'Toast',
  'alignRibbonModels',
  'audienceBasisLabel',
  'buildRibbonModel',
  'evidenceProofLine',
  'recommendationAllowed',
  'validateAudienceBasis',
  'validateEvidenceSummary',
  'validateRibbonSeries',
]);

const ENGINE_CONTRACT_VERSION = '1.0.0';

function unavailable(error){
  return {available: false, error};
}

function developmentError(code){
  console.error(`[instrument-contract] ${code}`);
}

function sha(value){
  return typeof value === 'string' && /^[0-9a-f]{40}$/i.test(value);
}

function exactExports(value){
  return Array.isArray(value)
    && value.length === REQUIRED_EXPORTS.length
    && value.every((name, index) => name === REQUIRED_EXPORTS[index]);
}

export function assertInstrumentPackageManifest(candidate){
  const boundary = safePlainData(candidate);
  const value = boundary.value;
  const valid = boundary.ok
    && value
    && typeof value === 'object'
    && value.package?.name === 'ogilvy-intelligence-design-system'
    && value.package?.version === '2.0.22'
    && value.contractVersion === '1.2.0'
    && exactExports(value.exportList)
    && sha(value.sourceCommit)
    && value.packageSourceCommit === value.sourceCommit
    && sha(value.evidenceCommit)
    && value.gateBEvidence?.receiptCommit === value.evidenceCommit
    && value.gateBEvidence?.sourceCommit === value.sourceCommit
    && value.gateBEvidence?.status === 'passed';

  if (!valid){
    developmentError('instrument_package_mismatch');
    return unavailable('instrument_package_mismatch');
  }
  return {available: true, error: null};
}

export function assertEngineContractCompatibility(metadata){
  const boundary = safePlainData(metadata);
  const value = boundary.value;
  const packageBoundary = safePlainData(manifest);
  const packageResult = packageBoundary.ok
    ? assertInstrumentPackageManifest(packageBoundary.value)
    : unavailable('instrument_package_mismatch');
  const valid = packageResult.available
    && boundary.ok
    && value
    && typeof value === 'object'
    && value.engineSourceSha === packageBoundary.value.sourceCommit
    && value.evidenceSummaryVersion === ENGINE_CONTRACT_VERSION
    && value.ribbonSeriesVersion === ENGINE_CONTRACT_VERSION
    && value.audienceBasisVersion === ENGINE_CONTRACT_VERSION;

  if (!valid){
    if (packageResult.available) developmentError('engine_contract_mismatch');
    return unavailable('engine_contract_mismatch');
  }
  return {available: true, error: null};
}
