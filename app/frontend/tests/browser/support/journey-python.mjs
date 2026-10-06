/* The Python a browser journey runs the real app under. The journeys that
   start the FastAPI app (the dossier and lens journeys) resolve it here, so
   they agree on the order: each named environment variable in turn, then
   app/.venv/bin/python, then python3 on the PATH. A candidate is only taken
   when it can import fastapi, since a system python3 without the app's
   dependencies would start no server and leave the journey waiting on it.
   When no candidate can, the journey fails at once and says what to set. */
import {execFileSync} from 'node:child_process';
import {existsSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const APP_ROOT = fileURLToPath(new URL('../../../../', import.meta.url));

export function journeyPythonCandidates(variables, environment = process.env, appRoot = APP_ROOT){
  const candidates = [];
  for (const name of variables){
    if (environment[name]) candidates.push({source: name, python: environment[name]});
  }
  const venv = join(appRoot, '.venv/bin/python');
  if (existsSync(venv)) candidates.push({source: 'app/.venv/bin/python', python: venv});
  candidates.push({source: 'python3 on the PATH', python: 'python3'});
  return candidates;
}

function importsFastapi(python, environment, appRoot){
  try {
    execFileSync(python, ['-c', 'import fastapi'], {cwd: appRoot, env: environment, stdio: 'ignore', timeout: 30000});
    return true;
  } catch (_error){
    return false;
  }
}

export function resolveJourneyPython(variables, environment = process.env, appRoot = APP_ROOT){
  const candidates = journeyPythonCandidates(variables, environment, appRoot);
  for (const candidate of candidates){
    if (importsFastapi(candidate.python, environment, appRoot)) return candidate.python;
  }
  const tried = candidates.map((candidate) => `${candidate.python} (${candidate.source})`).join(', ');
  throw new Error(
    `No Python for this journey can import fastapi. Tried: ${tried}. `
    + `Set ${variables[0]} to a Python with the app's dependencies installed, `
    + 'or create app/.venv with app/requirements.txt installed.',
  );
}
