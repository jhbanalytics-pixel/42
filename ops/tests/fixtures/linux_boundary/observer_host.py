import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from src.analysis.open_intelligence.brain_contract import canonical_bytes
from src.analysis.open_intelligence.general_question_store import GeneralQuestionStore


class ReadOnlyBlob:
    def __init__(self, record, updated):
        self.generation = record["generation"]
        self.raw = canonical_bytes(record["body"])
        self.size = len(self.raw)
        self.updated = updated

    def download_as_bytes(self, *, if_generation_match, raw_download, retry, **_):
        if if_generation_match != self.generation or raw_download is not True or retry is not None:
            raise ValueError("fixture_read_invalid")
        return self.raw


class ReadOnlyBucket:
    def __init__(self, fixture):
        self.name = fixture["bucket_name"]
        self.updated = datetime.fromisoformat(fixture["updated_at"].replace("Z", "+00:00"))
        self.records = {
            (record["name"], record["generation"]): record for record in fixture["objects"]
        }
        self.current = {record["name"]: record for record in fixture["objects"]}

    def get_blob(self, name, *, generation=None, retry=None, **_):
        if retry is not None:
            raise ValueError("fixture_read_invalid")
        record = self.current.get(name) if generation is None else self.records.get((name, generation))
        return None if record is None else ReadOnlyBlob(record, self.updated)

    def blob(self, *_args, **_kwargs):
        raise AssertionError("observer attempted a write")


def load_question_host(*, engine_root):
    path = Path(os.environ["R02_OBSERVER_STORE_PATH"])
    if not path.is_absolute() or not path.is_file():
        raise ValueError("fixture_path_invalid")
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if (
        fixture.get("contract_version") != "r02_linux_observer_store_v1"
        or set(fixture)
        != {
            "bucket_name",
            "contract_version",
            "deployment_digest",
            "objects",
            "policy",
            "updated_at",
        }
    ):
        raise ValueError("fixture_invalid")
    store = GeneralQuestionStore(
        ReadOnlyBucket(fixture),
        policy=fixture["policy"],
        deployment_digest=fixture["deployment_digest"],
    )
    return {
        "store": store,
        "runtime_identity": SimpleNamespace(engine_root=str(engine_root)),
        "credentials": None,
    }
