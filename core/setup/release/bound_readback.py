"""Read-only release readback helper (W8-REL 2.9).

    py -3.13 core/setup/release/bound_readback.py --mode services-only --phase <Phase> --bindings <file> --evidence <run dir>
        [--tag <release id>]

Exit 0 when every check passed, 1 (STOP) when a check failed, 3 (PROBE) when a read could not complete: it exceeded its
bound timeout, stalled, or came back unreadable. The two are never the same code. Every read, gcloud or HTTP, carries the
timeout the bindings bind (readTimeoutSeconds), so a stall ends in exit 3 and the operator re-runs within the deadline.
The helper calls only read commands. It never prints a token, a header or an environment value; a refusal prints its
stable code and a sentence naming a field.

`--mode full` is Release B's mode and is not carried here: a helper that trusts the latest ready revision and drops
traffic from its fingerprint must not run against a service this release has pinned, so asking for it is a STOP.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.setup.release import natives  # noqa: E402
from core.setup.release import services_only as so  # noqa: E402

PROJECT, REGION = so.PROJECT, so.REGION
# The gcloud commands this helper may issue. Anything else is refused before it runs.
READ_PREFIXES = (("config", "list"), ("run", "services", "describe"), ("run", "services", "get-iam-policy"),
                 ("run", "revisions", "describe"), ("run", "revisions", "list"), ("run", "jobs", "describe"),
                 ("artifacts", "docker", "images", "describe"), ("builds", "describe"), ("auth", "print-access-token"),
                 ("run", "jobs", "executions", "list"), ("run", "jobs", "executions", "describe"))


def native_program(name):
    """gcloud or git as the paste found it in its install folder (core/setup/release/natives.py); never a lookup on PATH."""
    try:
        return natives.native(name)
    except natives.NativeRefused as error:
        raise so.Stop("NATIVE", str(error)) from None


def gcloud_command():
    return [native_program("gcloud")]


# Every gcloud child keeps no file log, as the children of the release paste do: the arguments of a read can name what the release removes.
GCLOUD_ENV = {"CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "1"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class GcloudReader:
    """The real Reader. Every call is bounded by the bound timeouts and mapped to Probe when it cannot complete."""

    def __init__(self, timeouts):
        self.gcloud_timeout, self.http_timeout = timeouts["gcloud"], timeouts["http"]
        self.argv_log = []

    def _gcloud(self, args, *, raw=False):
        if not any(tuple(args[:len(prefix)]) == prefix for prefix in READ_PREFIXES):
            raise so.Stop("WRITE_REFUSED", "The helper issues read commands only")
        self.argv_log.append(list(args))
        try:
            result = subprocess.run([*gcloud_command(), *args], stdin=subprocess.DEVNULL, capture_output=True,
                                    encoding="utf-8", errors="strict", timeout=self.gcloud_timeout,
                                    env={**os.environ, **GCLOUD_ENV})
        except subprocess.TimeoutExpired:
            raise so.Probe("A read exceeded its bound timeout") from None
        except (UnicodeDecodeError, OSError):
            raise so.Probe("A read produced unreadable output") from None
        if result.returncode != 0:
            if "NOT_FOUND" in result.stderr or "not found" in result.stderr.lower():
                raise so.NotFound(" ".join(args[:3]))
            raise so.Probe(f"A read failed with exit {result.returncode}: {' '.join(args[:3])}")
        if raw:
            return result.stdout.strip()
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise so.Probe("A read produced unreadable output") from None

    def config(self):
        return self._gcloud(["config", "list", "--format=json(core.account,core.project,auth.impersonate_service_account)"])

    def service(self, name):
        return self._gcloud(["run", "services", "describe", name, "--project", PROJECT, "--region", REGION, "--format=json"])

    def revision(self, name):
        return self._gcloud(["run", "revisions", "describe", name, "--project", PROJECT, "--region", REGION, "--format=json"])

    def revisions(self, service):
        listed = self._gcloud(["run", "revisions", "list", "--service", service, "--project", PROJECT, "--region", REGION,
                               "--format=json"])
        return [r.get("metadata", {}).get("name") for r in listed]

    def job(self, name):
        return self._gcloud(["run", "jobs", "describe", name, "--project", PROJECT, "--region", REGION, "--format=json"])

    def executions(self, job):
        return self._gcloud(["run", "jobs", "executions", "list", "--job", job, "--project", PROJECT, "--region", REGION, "--format=json"])

    def execution(self, name):
        return self._gcloud(["run", "jobs", "executions", "describe", name, "--project", PROJECT, "--region", REGION, "--format=json"])

    def policy(self):
        return self._gcloud(["run", "services", "get-iam-policy", "f42-agent", "--project", PROJECT, "--region", REGION,
                             "--format=json"])

    def build(self, build_id):
        return self._gcloud(["builds", "describe", build_id, "--project", PROJECT, "--region", REGION, "--format=json"])

    def registry_digest(self, image_tag):
        try:
            value = self._gcloud(["artifacts", "docker", "images", "describe", image_tag, "--project", PROJECT, "--format=json"])
        except so.NotFound:
            return None
        return value.get("image_summary", {}).get("digest") or value.get("digest")

    def source_file_sha256(self, commit, path):
        try:
            result = subprocess.run([native_program("git"), "show", f"{commit}:{path}"], stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=self.gcloud_timeout)
        except (subprocess.TimeoutExpired, OSError):
            raise so.Probe("A read exceeded its bound timeout") from None
        if result.returncode != 0:
            raise so.NotFound(path)
        return so.sha_bytes(result.stdout.replace(b"\r\n", b"\n"))

    def anonymous_status(self, url):
        try:
            with urllib.request.build_opener(NoRedirect()).open(url.rstrip("/") + "/api/health", timeout=self.http_timeout) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except Exception:
            raise so.Probe("The anonymous boundary check could not complete") from None

    def health(self, url):
        try:
            with urllib.request.build_opener(NoRedirect()).open(url.rstrip("/") + "/api/health", timeout=self.http_timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, {}
        except Exception:
            raise so.Probe("The health read could not complete") from None

    # Principals are verified as the a80 helper does: ADC and the CLI token, each introspected at the issuer.
    def _adc_token(self):
        import concurrent.futures

        def read():
            import google.auth
            from google.auth.transport.requests import Request

            creds, project = google.auth.default()
            if not creds.valid:
                creds.refresh(Request())
            return creds.token, project, getattr(creds, "quota_project_id", None)

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(read).result(timeout=self.http_timeout)
        except concurrent.futures.TimeoutError:
            raise so.Probe("The ADC token read exceeded its bound timeout") from None
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _cli_token(self):
        return self._gcloud(["auth", "print-access-token", "--project", PROJECT, "--quiet"], raw=True)

    def _issuer_info(self, token):
        import requests

        with requests.Session() as session:
            response = session.get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": token},
                                   timeout=self.http_timeout, allow_redirects=False)
            return response.status_code, response.json() if response.status_code == 200 else {}

    def principals(self, bound):
        so.require(not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"), "IDENTITY", "An ADC credential-file override is outside the bound caller")
        for name in ("CLOUDSDK_AUTH_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE", "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
                     "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"):
            so.require(not os.environ.get(name), "IDENTITY", "A CLI credential override is outside the bound caller")
        previous = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                adc, project, quota = self._adc_token()
                cli = self._cli_token()
                so.require(isinstance(adc, str) and bool(adc) and isinstance(cli, str) and bool(cli), "IDENTITY",
                           "A current identity token is unavailable")
                so.require(project == PROJECT and quota == PROJECT, "IDENTITY", "The ADC project or quota project differs")
                adc_http, adc_info = self._issuer_info(adc)
                cli_http, cli_info = self._issuer_info(cli)
        except (so.Stop, so.Probe):
            raise
        except Exception as error:
            raise so.Stop("IDENTITY", "Current identity validation is unavailable: " + type(error).__name__) from None
        finally:
            logging.disable(previous)
        so.require(adc_http == 200 and cli_http == 200, "IDENTITY", "The token issuer identity is unavailable")
        subject = adc_info.get("sub") or adc_info.get("user_id")
        so.require(isinstance(subject, str) and bool(subject) and subject == (cli_info.get("sub") or cli_info.get("user_id")),
                   "IDENTITY", "The ADC and CLI principals differ or are unavailable")
        so.require(adc_info.get("email") == bound["callerAccount"] and cli_info.get("email") == bound["callerAccount"],
                   "IDENTITY", "The issuer-verified caller differs from the bound caller")
        for info in (adc_info, cli_info):
            for key in ("email_verified", "verified_email"):
                if key in info:
                    so.require(info[key] in (True, "true"), "IDENTITY", "The caller email is not issuer verified")
        return {"verified_principals_match": True}


def main(argv=None, reader_factory=None, bq_factory=None, now=None, head_files_factory=None):
    from core.setup.release import jobs_only as jo

    parser = argparse.ArgumentParser(description="Read-only release readback.")
    parser.add_argument("--mode", default="services-only", choices=("services-only", "jobs", "full"))
    parser.add_argument("--phase", choices=(*so.PHASES, *[p for p in jo.PHASES if p not in so.PHASES]), required=True)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--tag")
    args = parser.parse_args(argv)
    try:
        bound = json.loads(args.bindings.read_text(encoding="utf-8-sig"))
        so.require(args.mode in ("services-only", "jobs"), "MODE", "Only services-only and jobs modes are carried; full mode stays refused")
        so.require(isinstance(bound, dict) and bound.get("mode") == args.mode, "MODE", f"The bindings are not for {args.mode} mode")
        if args.mode == "jobs":
            so.require(args.phase in jo.PHASES and args.tag is None, "MODE", "That phase does not belong to jobs mode")
            jo.validate_jobs_bindings(bound)
            reader = (reader_factory or GcloudReader)(bound["readTimeoutSeconds"])
            client = (bq_factory or _default_bq)(bound)
            head_files = (lambda: head_files_factory(bound)) if head_files_factory else None
            result = jo.run_phase(bound, args.phase, reader, args.evidence, now=now, bq=client, head_files=head_files)
        else:
            so.require(args.phase in so.PHASES, "MODE", "That phase does not belong to services-only mode")
            so.validate_bindings(bound)
            reader = (reader_factory or GcloudReader)(bound["readTimeoutSeconds"])
            result = so.run_phase(bound, args.phase, reader, args.evidence, tag=args.tag)
    except so.Stop as error:
        print(f"STOP: {error.code}: {error.message}", file=sys.stderr)
        return 1
    except so.Probe as error:
        print(f"PROBE: {error}", file=sys.stderr)
        return 3
    except (KeyError, ValueError, OSError) as error:
        # A field name or a refusal reason is safe to print; raw subprocess text and credentials never are.
        print(f"STOP: BINDINGS: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"{args.phase}: passed ({result['file']})")
    return 0


def _default_bq(bound):
    from core.setup.release.chain_evidence import GoogleBq

    return GoogleBq(bound["readTimeoutSeconds"])


if __name__ == "__main__":
    raise SystemExit(main())
