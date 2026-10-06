import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

export const APPROVED = Object.freeze({
  archiveSha256: '2E86002374B1C4BA4127932D22319091EA9FCBC00443776D41E81DDD344276E6',
  contractVersion: '1.2.0',
  cssSha256: 'e39748fca5c56e7ca8a5fff28f566f7defd2e09641952d3757541f8ff077fbca',
  evidenceCommit: 'bf1e45a2e01e814ba47f64b307684d746f3e691f',
  packageSourceCommit: '61c42d1be2658e1f3dacdbf317683e81a86f4b6a',
  specSha256: '15765aebe3fa850f813c50e349699fe7e25f5036e27a74b01901beae0e291886',
  tokenDigest: 'f4fc6fab3594c27b923e5ecb727f8f9f759850a9845b351c36249a15ab482ed2', // gitleaks:allow, pinned non-secret contract digest
});

export const REQUIRED_EXPORTS = Object.freeze([
  'AudienceUnavailable', 'Banner', 'CitedAnswer', 'ComparisonInstrument', 'DiscoverInstrument',
  'EVIDENCE_STATES', 'EditorialLead', 'EmptyState', 'EvidenceDrawer', 'EvidenceLedger',
  'EvidenceReadiness', 'EvidenceRoom', 'FieldworkInstrument', 'HeldNotice', 'InstrumentBriefing',
  'InstrumentShell', 'IntelligenceConsole', 'MarketChip', 'MeasurementBands', 'MomentumPill',
  'OgilvyMasthead', 'OgilvyRoot', 'OgilvyShell', 'PlatformGlyph', 'ProofRibbon', 'Skeleton',
  'SourceInventoryRow', 'StateView', 'Toast', 'alignRibbonModels', 'audienceBasisLabel',
  'buildRibbonModel', 'evidenceProofLine', 'recommendationAllowed', 'validateAudienceBasis',
  'validateEvidenceSummary', 'validateRibbonSeries',
]);

const DEPENDENCY = 'file:vendor/ogilvy-intelligence-design-system-2.0.22.tgz';
const RESOLUTION = 'ogilvy-intelligence-design-system@vendor/ogilvy-intelligence-design-system-2.0.22.tgz';

function equalJson(left, right){
  return JSON.stringify(left) === JSON.stringify(right);
}

export function verifyInstrumentPackageInputs(input){
  const failures = [];
  const check = (name, condition) => { if (!condition) failures.push(name); };
  const {archivedManifest: manifest, expected} = input;
  check('archiveSha256', input.actualArchiveSha256 === expected.archiveSha256);
  check('packageIdentity', manifest.package?.name === 'ogilvy-intelligence-design-system' && manifest.package?.version === '2.0.22');
  check('packageSourceCommit', manifest.packageSourceCommit === expected.packageSourceCommit && manifest.sourceCommit === expected.packageSourceCommit);
  check('evidenceCommit', manifest.evidenceCommit === expected.evidenceCommit);
  check('specSha256', manifest.specSha256 === expected.specSha256);
  check('contractVersion', manifest.contractVersion === expected.contractVersion);
  check('tokenDigest', manifest.tokenDigest === expected.tokenDigest);
  check('exportList', equalJson(manifest.exportList, input.requiredExports));
  check('manifestCssSha256', manifest.cssSha256 === expected.cssSha256);
  check('styleSha256', input.styleSha256 === expected.cssSha256);
  check('styleInventory', manifest.files?.some((file) => file.path === 'style.css' && file.sha256 === expected.cssSha256));
  check('installedManifest', equalJson(input.installedManifest, manifest));
  check('packageDependency', input.packageDependency === DEPENDENCY);
  const resolutionHits = input.lockText.split(RESOLUTION).length - 1;
  const packageLockRecords = input.lockText.match(/ogilvy-intelligence-design-system@[^"\]]+/g) || [];
  check('consumerLock', resolutionHits === 1 && packageLockRecords.length === 1 && packageLockRecords[0] === RESOLUTION);
  if (failures.length) throw new Error(`Instrument package verification failed: ${failures.join(', ')}`);
  return {checkCount: 14, failures};
}

function sha256(bytes, uppercase = false){
  const digest = createHash('sha256').update(bytes).digest('hex');
  return uppercase ? digest.toUpperCase() : digest;
}

function archivedBytes(archivePath, member){
  const result = spawnSync('tar', ['-xOzf', archivePath, member], {encoding: null, windowsHide: true});
  if (result.status !== 0) throw new Error(`Could not read ${member}: ${result.stderr.toString('utf8')}`);
  return result.stdout;
}

export function verifyInstrumentPackage(frontendRoot){
  const archivePath = resolve(frontendRoot, 'vendor', 'ogilvy-intelligence-design-system-2.0.22.tgz');
  const archivedManifest = JSON.parse(archivedBytes(archivePath, 'package/dist/manifest.json').toString('utf8'));
  const styleBytes = archivedBytes(archivePath, 'package/dist/style.css');
  const packageJson = JSON.parse(readFileSync(resolve(frontendRoot, 'package.json'), 'utf8'));
  const installedManifest = JSON.parse(readFileSync(resolve(frontendRoot, 'node_modules', 'ogilvy-intelligence-design-system', 'dist', 'manifest.json'), 'utf8'));
  const result = verifyInstrumentPackageInputs({
    actualArchiveSha256: sha256(readFileSync(archivePath), true),
    archivedManifest,
    expected: APPROVED,
    installedManifest,
    lockText: readFileSync(resolve(frontendRoot, 'bun.lock'), 'utf8'),
    packageDependency: packageJson.dependencies?.['ogilvy-intelligence-design-system'],
    requiredExports: REQUIRED_EXPORTS,
    styleSha256: sha256(styleBytes),
  });
  return {...result, archiveBytes: readFileSync(archivePath).byteLength, manifest: archivedManifest};
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)){
  try {
    const result = verifyInstrumentPackage(resolve(fileURLToPath(new URL('..', import.meta.url))));
    console.log(JSON.stringify({archiveBytes: result.archiveBytes, checks: result.checkCount, failures: result.failures}));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
