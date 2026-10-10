"""The network and library doubles of the end to end release test (core/setup/tests/test_release_e2e.py).

Python imports a module called sitecustomize at start-up, so the test puts this folder on PYTHONPATH and every Python child of the
release paste (the readback helper, the durable effects checker, the smoke, the deploy script's snippets) loads it. It does nothing
unless F42_E2E_WORLD is set. It fakes only what reaches the cloud:

  urllib   the helper's anonymous and health reads, answered from the same simulated world the fake gcloud keeps
  httpx    the smoke's client, answered by a small f42-api that wants the passcode the operator typed
  google   ADC credentials and BigQuery, and the token issuer behind requests
  natives  the install folder gcloud is found in, so the fake is the file the resolver finds; git, py and bash stay real

The code under test (bound_readback.py, services_only.py, smoke.py, durable_effects_check.py, natives.py) is the repository's own and
runs unchanged.
"""
import importlib.abc
import importlib.machinery
import io
import json
import os
import sys
import types
import urllib.error
import urllib.request

WORLD = os.environ.get("F42_E2E_WORLD")


def _log(entry):
    with open(os.environ["F42_E2E_CALLS"], "a", encoding="utf-8") as out:
        out.write(json.dumps(entry) + "\n")


def _world():
    with open(WORLD, encoding="utf-8") as stream:
        return json.load(stream)


# the version a URL serves, read from the revision that serves it

def _serving_version(world, url):
    base = url.rstrip("/")
    if base.endswith("/api/health"):
        base = base[: -len("/api/health")]
    for service in ("f42-api", "f42-agent"):
        state = world["svc"][service]
        revision = None
        if base == state["url"]:
            entry = next((e for e in state["traffic"] if e.get("percent") == 100), None)
            if entry is not None:
                revision = state["latest_ready"] if entry.get("latestRevision") else entry["revisionName"]
        elif "---" in base:
            tag, _, host = base[len("https://"):].partition("---")
            if "https://" + host == state["url"]:
                entry = next((e for e in state["traffic"] if e.get("tag") == tag), None)
                revision = entry["revisionName"] if entry else None
        if revision:
            env = world["revisions"][revision]["spec"]["containers"][0]["env"]
            return service, next((e.get("value") for e in env if e["name"] == "F42_VERSION"), None)
    return None, None


class _Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode("utf-8"))
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _Opener:
    def open(self, url, data=None, timeout=None):
        service, version = _serving_version(_world(), url)
        if service == "f42-agent":
            _log({"tool": "http", "url": url, "status": 403})
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        if service is None:
            _log({"tool": "http", "url": url, "status": "unreachable"})
            raise urllib.error.URLError("no such host in the simulated world")
        _log({"tool": "http", "url": url, "status": 200})
        return _Response(200, {"ok": True, "checks": {"agent": "ok"}, "version": version})


def _install_urllib():
    urllib.request.build_opener = lambda *handlers: _Opener()


# the smoke's f42-api

def _smoke_handler(httpx):
    root = os.environ["F42_E2E_FIXTURES"]
    with open(os.path.join(root, "ask_complete.json"), encoding="utf-8") as stream:
        record = json.load(stream)
    record["ask_id"] = "a_e2e_0001"
    wire = {"check": "verified", "v": 1, "execution": {"state": "completed", "stop_reason": None},
            "summary": {"state": "shown", "removals": [], "rewrite": "not_attempted"}}
    markets = [{"market": code, "status": "ready", "cards": [{"id": f"{code}-1"}]} for code in ("ZA", "NG", "KE")]

    def handle(request):
        path = request.url.path
        allowed = request.headers.get("x-passcode") == os.environ["F42_E2E_PASSCODE"]
        seen = {"tool": "http", "method": request.method, "url": str(request.url), "passcode_accepted": allowed}
        if path == "/api/health":
            service, version = _serving_version(_world(), f"{request.url.scheme}://{request.url.host}")
            response = httpx.Response(200, json={"ok": True, "checks": {"agent": "ok"}, "version": version})
        elif not allowed:
            response = httpx.Response(401, json={"error": "unauthorised", "message": "passcode"})
        elif path == "/api/today":
            response = httpx.Response(200, json={"date": "2026-10-10", "status": "ready", "markets": markets,
                                                 "published_at": "2026-10-10T05:00:00+00:00"})
        elif request.method == "POST" and path == "/api/ask":
            seen["body"] = json.loads(request.content)
            response = httpx.Response(202, json={"ask_id": record["ask_id"], "status": "running"})
        elif path == f"/api/ask/{record['ask_id']}":
            response = httpx.Response(200, json=dict(record, status="complete", answer_meta=wire))
        elif path == f"/api/ask/{record['ask_id']}/events":
            response = httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      text='event: step\ndata: {"n": 1}\n\nevent: done\ndata: {"status": "complete"}\n\n')
        elif path == f"/api/ask/{record['ask_id']}/export":
            response = httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text="<html><body>export</body></html>")
        else:
            response = httpx.Response(404, json={"error": "not_found", "message": "no route"})
        seen["status"] = response.status_code
        _log(seen)
        return response

    return handle


def _patch_httpx(module):
    original = module.Client.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("transport", module.MockTransport(_smoke_handler(module)))
        original(self, *args, **kwargs)

    module.Client.__init__ = init


# Google libraries and the token issuer

def _install_google():
    def package(name):
        module = types.ModuleType(name)
        module.__path__ = []
        sys.modules[name] = module
        return module

    google, auth, transport = package("google"), package("google.auth"), package("google.auth.transport")
    request_module, cloud, bigquery = types.ModuleType("google.auth.transport.requests"), package("google.cloud"), types.ModuleType("google.cloud.bigquery")
    for module in (request_module, bigquery):
        sys.modules[module.__name__] = module
    google.auth, google.cloud = auth, cloud
    auth.transport, transport.requests, cloud.bigquery = transport, request_module, bigquery
    auth.default = lambda *a, **k: (types.SimpleNamespace(valid=True, token="e2e-adc-token", quota_project_id="ogilvy-trends-v2"), "ogilvy-trends-v2")
    request_module.Request = type("Request", (), {})

    class QueryJobConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Client:
        def __init__(self, project=None):
            self.project = project

        def query(self, sql, job_config=None):
            _log({"tool": "bigquery", "dry_run": bool(getattr(job_config, "dry_run", False))})
            rows = [{"running": int(os.environ.get("F42_E2E_INFLIGHT", "0"))}]
            return types.SimpleNamespace(total_bytes_processed=1000, result=lambda: rows)

    bigquery.Client, bigquery.QueryJobConfig = Client, QueryJobConfig

    requests = types.ModuleType("requests")

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None, timeout=None, allow_redirects=True):
            _log({"tool": "http", "url": url, "status": 200})
            info = {"sub": "e2e-subject", "email": os.environ["F42_E2E_CALLER"], "email_verified": "true"}
            return types.SimpleNamespace(status_code=200, json=lambda: info)

    requests.Session = Session
    sys.modules["requests"] = requests


# natives: the fake gcloud is the file the resolver finds

def _patch_natives(module):
    original = module.install_folders

    def install_folders(name, roots=None, windows=None):
        if name == "gcloud":
            return [os.environ["F42_E2E_GCLOUD_DIR"]]
        return original(name, roots, windows)

    module.install_folders = install_folders


class _After(importlib.abc.MetaPathFinder):
    def __init__(self, name, patch):
        self.name, self.patch = name, patch

    def find_spec(self, fullname, path, target=None):
        if fullname != self.name:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        loader, execute = spec.loader, spec.loader.exec_module

        def exec_module(module):
            execute(module)
            self.patch(module)

        loader.exec_module = exec_module
        return spec


if WORLD:
    _install_urllib()
    _install_google()
    sys.meta_path.insert(0, _After("httpx", _patch_httpx))
    sys.meta_path.insert(0, _After("core.setup.release.natives", _patch_natives))
