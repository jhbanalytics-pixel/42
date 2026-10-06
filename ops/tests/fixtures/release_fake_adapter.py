"""Local double for the native clients consumed by the release lifecycle.

The state lives in one JSON file so a subprocess invocation of release.py sees
the effect of the previous command. Every mutating call is journaled. Nothing
here talks to a network.
"""

import base64
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path


class PreconditionFailed(Exception):
    pass


class FakeState:
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_bytes())
        self.data.setdefault("journal", [])
        self.data.setdefault("reads", [])
        self.data.setdefault("failures", {})
        self.data.setdefault("operations", {})
        self.data.setdefault("revisions", {})
        self.data.setdefault("objects", {})
        self.data.setdefault("bytes", {})
        self.data.setdefault("next_generation", 100)
        self.data.setdefault(
            "clock", {"now": "2026-09-13T08:00:00+00:00", "monotonic": 0.0}
        )

    def save(self):
        self.path.write_text(
            json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8"
        )

    def journal(self, entry):
        self.data["journal"].append(entry)
        self.save()

    def mutations(self):
        return [entry for entry in self.data["journal"] if entry["call"] != "guard"]


def encode_raw(raw):
    return base64.b64encode(raw).decode("ascii")


def decode_raw(text):
    return base64.b64decode(text.encode("ascii"), validate=True)


class _Clock:
    def __init__(self, state):
        self.state = state

    def now(self):
        return datetime.fromisoformat(
            self.state.data["clock"]["now"].replace("Z", "+00:00")
        )

    def monotonic(self):
        return float(self.state.data["clock"]["monotonic"])

    def sleep(self, seconds):
        self.state.data["clock"]["monotonic"] += float(seconds)
        self.state.save()


class _Run:
    def __init__(self, state):
        self.state = state

    def get_service(self, name):
        service = self.state.data["service"]
        return copy.deepcopy(service) if service["name"] == name else None

    def get_revision(self, name):
        return copy.deepcopy(self.state.data["revisions"].get(name))

    def _operation(self, kind, failure):
        operations = self.state.data["operations"]
        name = f"{self.state.data['service']['name']}/operations/{kind}-{len(operations) + 1}"
        pending = int(self.state.data["failures"].get(f"{kind}_pending_reads", 0))
        operations[name] = {
            "state": "failed" if failure == "failed" else "succeeded",
            "pending_reads": pending,
            "error": f"{kind}_failed" if failure == "failed" else None,
        }
        return name

    def deploy_revision(
        self,
        service_name,
        *,
        revision,
        template,
        idempotency_key,
        expected_current_template=None,
        expected_current_service=None,
    ):
        if (
            expected_current_template is not None
            and self.state.data["service"]["template"] != expected_current_template
        ):
            raise PreconditionFailed("pricing_service_drift")
        if expected_current_service is not None:
            current = self.state.data["service"]
            if current.get("reconciling", False) != expected_current_service.get(
                "reconciling", False
            ) or any(
                current.get(field) != expected_current_service.get(field)
                for field in (
                    "terminalCondition",
                    "generation",
                    "observedGeneration",
                    "latestCreatedRevision",
                    "latestReadyRevision",
                    "traffic",
                    "trafficStatuses",
                )
            ):
                raise PreconditionFailed("pricing_service_drift")
        self.state.journal(
            {
                "call": "deploy_revision",
                "service": service_name,
                "revision": revision,
                "idempotency_key": idempotency_key,
            }
        )
        failure = self.state.data["failures"].get("deploy_revision")
        if failure == "raise":
            raise RuntimeError("deploy_unavailable")
        name = self._operation("deploy", failure)
        if failure != "failed":
            service = self.state.data["service"]
            full = f"{service_name}/revisions/{revision}"
            self.state.data["revisions"][full] = {
                "name": full,
                "serviceAccount": template["serviceAccount"],
                "containers": copy.deepcopy(template["containers"]),
                "timeout": template["timeout"],
                "scaling": copy.deepcopy(template["scaling"]),
                "conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}],
            }
            service["template"] = copy.deepcopy(template)
            service["template"]["revision"] = revision
            service["latestCreatedRevision"] = full
            if "generation" in service:
                service["generation"] = str(int(service["generation"]) + 1)
                service["observedGeneration"] = service["generation"]
                service["latestReadyRevision"] = full
                service["reconciling"] = False
            service["etag"] = "etag-" + hashlib.sha256(full.encode()).hexdigest()[:12]
        self.state.save()
        return {"operation": name}

    def update_traffic(self, service_name, *, etag, traffic, idempotency_key):
        self.state.journal(
            {
                "call": "update_traffic",
                "service": service_name,
                "etag": etag,
                "traffic": traffic,
                "idempotency_key": idempotency_key,
            }
        )
        service = self.state.data["service"]
        if etag != service["etag"]:
            raise PreconditionFailed("etag_mismatch")
        failure = self.state.data["failures"].get("update_traffic")
        if failure == "raise":
            raise RuntimeError("traffic_unavailable")
        name = self._operation("traffic", failure)
        if failure != "failed":
            service["traffic"] = copy.deepcopy(traffic)
            service["trafficStatuses"] = [
                {
                    "type": item["type"],
                    "revision": item["revision"],
                    "percent": item["percent"],
                    "tag": item["tag"],
                }
                for item in traffic
            ]
            service["etag"] = (
                "etag-" + hashlib.sha256(json.dumps(traffic).encode()).hexdigest()[:12]
            )
        self.state.save()
        return {"operation": name}

    def read_operation(self, name, timeout_seconds):
        self.state.data["reads"].append(
            {"operation": name, "timeout_seconds": timeout_seconds}
        )
        operation = self.state.data["operations"].get(name)
        if operation is None:
            self.state.save()
            return {
                "operation": name,
                "state": "unknown",
                "error": "operation_not_found",
            }
        if operation["pending_reads"] > 0:
            operation["pending_reads"] -= 1
            self.state.save()
            return {"operation": name, "state": "pending"}
        self.state.save()
        return {
            "operation": name,
            "state": operation["state"],
            "error": operation["error"],
        }


class _Tasks:
    def __init__(self, state):
        self.state = state

    def get_queue(self, name):
        queue = self.state.data["queue"]
        return copy.deepcopy(queue) if queue["name"] == name else None

    def list_tasks(self, name, *, page_token=None):
        if self.state.data.get("tasks_endless_pages"):
            return {"tasks": [], "nextPageToken": "again"}
        tasks = [
            task
            for task in self.state.data["tasks"]
            if task["name"].startswith(name + "/")
        ]
        size = int(self.state.data.get("tasks_page_size", 1000))
        start = int(page_token or 0)
        page = {"tasks": copy.deepcopy(tasks[start : start + size])}
        if start + size < len(tasks):
            page["nextPageToken"] = str(start + size)
        return page

    def pause(self, name):
        self.state.journal({"call": "pause_queue", "queue": name})
        if self.state.data["failures"].get("pause_queue") != "ignored":
            self.state.data["queue"]["state"] = "PAUSED"
        self.state.save()
        return copy.deepcopy(self.state.data["queue"])

    def resume(self, name):
        self.state.journal({"call": "resume_queue", "queue": name})
        if self.state.data["failures"].get("resume_queue") == "raise":
            raise RuntimeError("resume_unavailable")
        self.state.data["queue"]["state"] = "RUNNING"
        self.state.save()
        return copy.deepcopy(self.state.data["queue"])


class _Objects:
    def __init__(self, state):
        self.state = state

    def read(self, name, *, generation=None):
        stored = self.state.data["objects"].get(name)
        if stored is None:
            return None
        if generation is not None and str(generation) != stored["generation"]:
            return None
        found = {
            "generation": stored["generation"],
            "raw": decode_raw(stored["raw_b64"]),
        }
        if "time_created" in stored:
            found["time_created"] = stored["time_created"]
        return found

    def create(self, name, raw, *, if_generation_match):
        stored = self.state.data["objects"].get(name)
        failures = self.state.data["failures"]
        if stored is not None and failures.get("create_object") == "concurrent_write":
            failures.pop("create_object")
            stored["generation"] = str(self.state.data["next_generation"])
            self.state.data["next_generation"] += 1
            self.state.journal(
                {
                    "call": "concurrent_write",
                    "name": name,
                    "generation": stored["generation"],
                }
            )
        self.state.journal(
            {
                "call": "create_object",
                "name": name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "if_generation_match": if_generation_match,
            }
        )
        current = int(stored["generation"]) if stored else 0
        if int(if_generation_match) != current:
            raise PreconditionFailed("generation_mismatch")
        generation = str(self.state.data["next_generation"])
        self.state.data["next_generation"] += 1
        self.state.data["objects"][name] = {
            "generation": generation,
            "raw_b64": encode_raw(raw),
            "time_created": self.state.data["clock"]["now"],
        }
        self.state.save()
        return {"generation": generation}


BUCKET = "listening-post-staging-cache"
OBJECT_PREFIX = "open-intelligence/v2/staging/general-questions/"


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class _Bytes:
    """Models the native reader's reference grammar over the state file.

    ``read`` answers a bare reference from the ``bytes`` map, which is how a test
    serves a ``source:`` page. ``reader`` binds an index and resolves the same
    kinds the native reader does: ``object:`` references and the index's
    ``evidence_objects`` from the ``objects`` store by name and generation,
    ``policy`` and ``deployment`` as the digest preimage of the stored policy and
    binding objects, and ``commit``, the two images and ``asset:`` paths from the
    ``bytes`` map keyed by reference, standing in for Cloud Build, the registry
    and the tagged revision.
    """

    def __init__(self, state, objects):
        self.state = state
        self.objects = objects

    def read(self, reference):
        stored = self.state.data["bytes"].get(reference)
        return None if stored is None else decode_raw(stored)

    def _object(self, reference):
        uri, marker, generation = reference[len("object:") :].rpartition("#")
        if not marker or not uri.startswith("gs://") or not generation.isdigit():
            return None
        bucket, slash, name = uri[len("gs://") :].partition("/")
        if not slash or not bucket or not name:
            return None
        if bucket != BUCKET:
            raise ValueError("object_bucket_not_approved")
        stored = self.objects.read(name, generation=generation)
        return None if stored is None else stored["raw"]

    def _preimage(self, name, field):
        stored = self.objects.read(name)
        if stored is None:
            return None
        value = json.loads(stored["raw"].decode("utf-8"))
        if type(value) is not dict or field not in value:
            return None
        return _canonical({key: item for key, item in value.items() if key != field})

    def reader(self, index, *, asset_origin):
        self.state.data["asset_origin"] = asset_origin
        self.state.save()

        def bound(reference):
            if not isinstance(reference, str):
                return None
            if reference.startswith("object:"):
                return self._object(reference)
            evidence = index.get("evidence_objects")
            if isinstance(evidence, dict) and reference in evidence:
                item = evidence[reference]
                return self._object(f"object:{item['uri']}#{item['generation']}")
            if reference == "policy":
                return self._preimage(
                    OBJECT_PREFIX + f"policies/{index['policy_digest']}/policy.json",
                    "policy_digest",
                )
            if reference == "deployment":
                return self._preimage(
                    OBJECT_PREFIX
                    + f"deployments/{index['deployment_digest']}/binding.json",
                    "deployment_digest",
                )
            if reference == "commit" or reference in ("engine_image", "app_image"):
                return self.read(reference)
            if reference.startswith("asset:"):
                return self.read(reference)
            return None

        return bound


def clients(state_path):
    state = FakeState(state_path)
    objects = _Objects(state)
    return {
        "run": _Run(state),
        "tasks": _Tasks(state),
        "objects": objects,
        "bytes": _Bytes(state, objects),
        "clock": _Clock(state),
        "_state": state,
    }
