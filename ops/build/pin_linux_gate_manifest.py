"""Repin the digest-bearing fields of the Linux gate manifest at the current tree.

Only digests change. Every other field is copied through byte for byte, and the
closure path sets must match what the gate consumers pin: the literals in
app/tests/unit/test_engine_observer_subprocess.py and the COPY lines in
ops/tests/Dockerfile.linux-gates.
"""

import argparse
import copy
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = "ops/tests/fixtures/linux_boundary/manifest.json"
BUILDER_PATH = "app/scripts/build_general_question_context.py"
PUBLISH_CONFIG_PATH = "cloudbuild.app.publish.yaml"
PUBLISH_GATE_STEP = "verify_linux_boundary_tests"
# A 64-hex literal that is not the digest of an image reference.
DIGEST_LITERAL = re.compile(r"(?<!@sha256:)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_BUNDLE_SCRIPT = (
    "import importlib.util, json, sys\n"
    "spec = importlib.util.spec_from_file_location('pin_linux_gate_manifest', sys.argv[1])\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
    "if sys.argv[3] == 'inventory':\n"
    "    value = module.engine_bundle_in_process(sys.argv[2], include_files=True)\n"
    "else:\n"
    "    value = module.engine_bundle_in_process(sys.argv[2])\n"
    "print(json.dumps(value))\n"
)
BUNDLE_CONTRACT = "general_question_engine_bundle_v1"
ENGINE_BUNDLE_ENTRYPOINT = "engine/scripts/staging/run_general_question_worker.py"
ENGINE_BUNDLE_DIGEST_KEYS = (
    "file_count",
    "files_map_sha256",
    "bundle_manifest_sha256",
    "entrypoint_sha256",
)
CLOSURE_BASES = {
    "test_dependency_closure": "",
    "app_test_closure": "",
    "engine_test_closure": "engine",
    "adapter_hashes": "ops/tests/fixtures/linux_boundary",
    "pdf_runtime_closure": "",
    "pdf_test_closure": "",
}
DIGEST_SECTIONS = frozenset(CLOSURE_BASES) | {"engine_bundle"}
EXPECTED_PATHS = {
    "test_dependency_closure": frozenset(
        {"app/requirements-dev.lock", "engine/requirements-dev.lock"}
    ),
    "app_test_closure": frozenset(
        {
            "app/conftest.py",
            "app/src/__init__.py",
            "app/src/api/__init__.py",
            "app/src/api/fieldwork.py",
            "app/src/api/question_worker_bundle.py",
            "app/src/api/question_worker_controller.py",
            "app/src/api/question_worker_deadline.py",
            "app/src/api/question_worker_process.py",
            "app/src/api/question_worker_protocol.py",
            "app/src/api/question_worker_result.py",
            "app/src/api/question_worker_store.py",
            "app/tests/unit/test_question_worker_bundle.py",
            "app/tests/unit/test_engine_observer_subprocess.py",
            "app/tests/unit/test_question_worker_controller.py",
            "app/tests/unit/test_question_worker_deadline.py",
            "app/tests/unit/test_question_worker_process.py",
            "app/tests/unit/test_question_worker_protocol.py",
            "app/tests/unit/test_question_worker_result.py",
            "app/tests/unit/test_question_worker_store.py",
            "app/tests/fixtures/general-question-result-unavailable.json",
        }
    ),
    "engine_test_closure": frozenset(
        {
            "tests/__init__.py",
            "tests/conftest.py",
            "tests/unit/__init__.py",
            "tests/unit/test_general_question_admission.py",
            "tests/unit/test_general_question_answer.py",
            "tests/unit/test_general_question_calls.py",
            "tests/unit/test_general_question_deployment.py",
            "tests/unit/test_general_question_observation.py",
            "tests/unit/test_general_question_plan.py",
            "tests/unit/test_general_question_policy.py",
            "tests/unit/test_general_question_result.py",
            "tests/unit/test_general_question_runtime.py",
            "tests/unit/test_general_question_store.py",
            "tests/unit/test_general_question_worker_cli.py",
        }
    ),
    "adapter_hashes": frozenset(
        {"observer_host.py", "observer_store.json", "sitecustomize.py"}
    ),
    "pdf_runtime_closure": frozenset(
        {
            "app/configs/pdf_runtime.json",
            "app/src/api/pdf_exporter.py",
            "app/src/api/pdf_renderer_worker.py",
        }
    ),
    "pdf_test_closure": frozenset(
        {
            "app/tests/fixtures/pdf/newsreader-chromium151.pdf",
            "app/tests/unit/test_pdf_renderer_linux.py",
        }
    ),
}


class ManifestFormatError(ValueError):
    """The manifest cannot be rendered back to its own bytes."""


class ClosurePathError(ValueError):
    """A closure names a path set the consumers do not expect, or a missing file."""


class ProtectedFieldError(ValueError):
    """A write asked to change a field that carries no digest."""


class PublishConfigError(ValueError):
    """The publish build config does not carry exactly one gate digest."""


class EngineBundleError(ValueError):
    """The builder could not inventory engine/ in a fresh interpreter."""


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def render(manifest, indent):
    return (json.dumps(manifest, indent=indent, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def load_manifest(path):
    raw = Path(path).read_bytes()
    manifest = json.loads(raw)
    indented = [line for line in raw.decode("utf-8").splitlines() if line[:1] == " "]
    indent = len(indented[0]) - len(indented[0].lstrip(" ")) if indented else 2
    if render(manifest, indent) != raw:
        raise ManifestFormatError("manifest_format")
    for section, expected in EXPECTED_PATHS.items():
        names = set(manifest[section])
        if names != expected:
            unexpected = sorted((names ^ expected))[0]
            raise ClosurePathError(f"closure_paths_mismatch {section} {unexpected}")
    if manifest["engine_bundle"]["entrypoint"] != ENGINE_BUNDLE_ENTRYPOINT:
        raise ClosurePathError("entrypoint_mismatch")
    return manifest, indent


def _builder(root):
    spec = importlib.util.spec_from_file_location(
        "build_general_question_context", Path(root) / BUILDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_builder(root, mode):
    root = Path(root).resolve()
    command = [sys.executable, "-I", "-c", _BUNDLE_SCRIPT, __file__, str(root), mode]
    result = subprocess.run(command, cwd=root / "app", capture_output=True, timeout=600)
    if result.returncode:
        lines = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = lines[-1] if lines else f"exit {result.returncode}"
        raise EngineBundleError(f"engine_bundle_unavailable {detail}")
    return json.loads(result.stdout.decode("utf-8"))


def compute_engine_bundle(root):
    """Run the bundle computation in a fresh interpreter, so no src package collides."""
    return _run_builder(root, "bundle")


def engine_bundle_in_process(root, *, include_files=False):
    """Reproduce the builder's bundle manifest for engine/ and digest it."""
    builder = _builder(root)
    files = builder.inventory_source(Path(root) / "engine", kind="engine")["files"]
    manifest = {"contract_version": BUNDLE_CONTRACT, "files": files}
    files_map = json.dumps(
        files,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    bundle = {
        "file_count": len(files),
        "files_map_sha256": _sha256(files_map),
        "bundle_manifest_sha256": _sha256(builder.canonical(manifest)),
        "entrypoint_sha256": _sha256(
            (Path(root) / ENGINE_BUNDLE_ENTRYPOINT).read_bytes()
        ),
    }
    return {"bundle": bundle, "files": files} if include_files else bundle


def compute_closure(root, manifest, section):
    """Digest every path a closure names; a missing file maps to None."""
    base = Path(root) / CLOSURE_BASES[section]
    digests = {}
    for name in manifest[section]:
        path = base / name
        digests[name] = _sha256(path.read_bytes()) if path.is_file() else None
    return digests


def compute_all(root, manifest):
    computed = {
        section: compute_closure(root, manifest, section) for section in CLOSURE_BASES
    }
    computed["engine_bundle"] = compute_engine_bundle(root)
    return computed


def stale_entries(manifest, computed):
    """Every (section, path, pinned, actual) that does not bind."""
    stale = []
    for section, values in computed.items():
        for name, actual in values.items():
            pinned = manifest[section][name]
            if pinned != actual:
                stale.append((section, name, pinned, actual))
    return stale


def apply_digests(manifest, computed):
    """Return a copy with only digest-bearing fields replaced."""
    updated = copy.deepcopy(manifest)
    for section, values in computed.items():
        if section not in DIGEST_SECTIONS:
            raise ProtectedFieldError(f"protected_field {section}")
        for name, value in values.items():
            if section == "engine_bundle" and name not in ENGINE_BUNDLE_DIGEST_KEYS:
                raise ProtectedFieldError(f"protected_field engine_bundle.{name}")
            if section != "engine_bundle" and name not in manifest[section]:
                raise ClosurePathError(f"closure_paths_mismatch {section} {name}")
            if value is None:
                raise ClosurePathError(f"closure_file_missing {section} {name}")
            updated[section][name] = value
    for key in manifest:
        if key not in DIGEST_SECTIONS and render(updated[key], 2) != render(
            manifest[key], 2
        ):
            raise ProtectedFieldError(f"protected_field {key}")
    return updated


def _publish_steps(text):
    """Split the steps sequence into one text block per step, without a parser."""
    lines = text.splitlines(keepends=True)
    start = next(
        (index for index, line in enumerate(lines) if line.rstrip("\r\n") == "steps:"),
        None,
    )
    if start is None:
        return []
    steps, item_indent = [], None
    for line in lines[start + 1 :]:
        content = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not content or content.startswith("#"):
            if steps:
                steps[-1].append(line)
            continue
        if item_indent is None:
            if not content.startswith("- "):
                break
            item_indent = indent
        if indent < item_indent or (
            indent == item_indent and not content.startswith("- ")
        ):
            break
        if indent == item_indent:
            steps.append([line])
        else:
            steps[-1].append(line)
    return ["".join(step) for step in steps]


def _args_block(step):
    """The text of a step's args mapping value, inline or nested."""
    lines = step.splitlines(keepends=True)
    for index, line in enumerate(lines):
        stripped = line.lstrip(" ")
        key_indent = len(line) - len(stripped)
        if stripped.startswith("- "):
            stripped, key_indent = stripped[2:].lstrip(" "), key_indent + 2
        if stripped.rstrip("\r\n") == "args:" or stripped.startswith("args: "):
            block = [line]
            for rest in lines[index + 1 :]:
                content = rest.strip()
                if content and len(rest) - len(rest.lstrip(" ")) <= key_indent:
                    break
                block.append(rest)
            return "".join(block)
    return ""


def publish_config_pin(raw, expected):
    """The manifest digest the verify step's args carry.

    The expected digest wins when the step carries it; otherwise the step's one
    other digest literal is the stale pin. Either must appear once in the args
    and nowhere else in the file outside image references.
    """
    text = raw.decode("utf-8")
    pattern = rf"^\s*-?\s*id:\s*['\"]?{PUBLISH_GATE_STEP}['\"]?\s*$"
    gate = [step for step in _publish_steps(text) if re.search(pattern, step, re.M)]
    if len(gate) != 1:
        raise PublishConfigError("publish_config_digest_absent")
    in_args = DIGEST_LITERAL.findall(_args_block(gate[0]))
    candidates = [expected] if expected in in_args else sorted(set(in_args))
    if not candidates:
        raise PublishConfigError("publish_config_digest_absent")
    if len(candidates) != 1:
        raise PublishConfigError("publish_config_digest_ambiguous")
    pin = candidates[0]
    if in_args.count(pin) != 1 or DIGEST_LITERAL.findall(text).count(pin) != 1:
        raise PublishConfigError("publish_config_digest_ambiguous")
    return pin


def _replace_pin(raw, pin, digest):
    """Replace the one non-image occurrence of pin, leaving every other byte."""
    text = raw.decode("utf-8")
    matches = [match for match in DIGEST_LITERAL.finditer(text) if match.group() == pin]
    if len(matches) != 1:
        raise PublishConfigError("publish_config_digest_ambiguous")
    start, end = matches[0].span()
    return (text[:start] + digest + text[end:]).encode("utf-8")


def _publish_config(root, publish_config):
    path = Path(publish_config)
    if not path.is_absolute():
        path = Path(root) / path
    try:
        display = path.relative_to(Path(root)).as_posix()
    except ValueError:
        display = path.as_posix()
    return path, display


def _publish_pin(config, expected, display):
    try:
        return publish_config_pin(config, expected)
    except PublishConfigError as error:
        raise PublishConfigError(f"{error} {display}") from None


def _display(value):
    return str(value)[:12]


class CheckoutBytesError(ValueError):
    """The checkout cannot supply reviewed HEAD bytes for a digest."""


def _head_blobs(root, names):
    names = sorted(names)
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if head.returncode:
            raise CheckoutBytesError("pin_tool_head_unavailable")
        result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "--batch"],
            input="".join(f"HEAD:{name}\n" for name in names).encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CheckoutBytesError("pin_tool_head_unavailable") from None
    if result.returncode:
        raise CheckoutBytesError("pin_tool_head_unavailable")
    blobs, missing, offset = {}, [], 0
    for name in names:
        end = result.stdout.find(b"\n", offset)
        header = result.stdout[offset:end].split()
        offset = end + 1
        if end < 0 or len(header) != 3 or header[1] != b"blob":
            missing.append(name)
            continue
        size = int(header[2])
        blobs[name] = result.stdout[offset : offset + size]
        offset += size + 1
    if missing:
        raise CheckoutBytesError("pin_tool_untracked_input " + " ".join(missing))
    return blobs


def _checkout_guard(root, manifest_path, manifest, stale=(), *, preflight=False):
    root = Path(root).resolve()
    try:
        manifest_name = Path(manifest_path).resolve().relative_to(root).as_posix()
    except ValueError:
        raise CheckoutBytesError(
            "pin_tool_head_unavailable manifest_outside_root"
        ) from None
    _head_blobs(root, [manifest_name])
    snapshot = _run_builder(root, "inventory")
    inventory = snapshot["files"]
    bundle_paths = {f"engine/{name}" for name in inventory}
    paths = {manifest_name, *bundle_paths}
    for section, base in CLOSURE_BASES.items():
        paths.update((Path(base) / name).as_posix() for name in manifest[section])
    # Missing closure files retain the existing missing-file refusal.
    present = {name for name in paths if (root / name).is_file()}
    blobs = _head_blobs(root, present)
    movable = {
        (Path(CLOSURE_BASES[section]) / name).as_posix()
        for section, name, _, _ in stale
        if section in CLOSURE_BASES
    }
    if any(
        section == "engine_bundle" and name == "files_map_sha256"
        for section, name, _, _ in stale
    ):
        movable.update(bundle_paths)
    if preflight:
        movable.update(paths - {manifest_name})
    fingerprints = dict.fromkeys(paths)
    crlf, dirty, modified = [], [], []
    for name in sorted(present):
        raw = (root / name).read_bytes()
        fingerprints[name] = _sha256(raw)
        if fingerprints[name] == _sha256(blobs[name]):
            continue
        text_crlf = False
        if b"\x00" not in raw:
            try:
                raw.decode("utf-8")
                text_crlf = b"\r\n" in raw
            except UnicodeDecodeError:
                pass
        if text_crlf:
            crlf.append(name)
        elif name not in movable:
            dirty.append(name)
        else:
            modified.append(name)
    if crlf:
        raise CheckoutBytesError("pin_tool_crlf_checkout " + " ".join(crlf))
    if dirty:
        raise CheckoutBytesError(
            "pin_tool_checkout_dirty " + " ".join(dirty) + " differs_from_HEAD"
        )
    if any(
        fingerprints[f"engine/{name}"] != digest for name, digest in inventory.items()
    ):
        raise CheckoutBytesError("pin_tool_checkout_changed")
    if (
        fingerprints[ENGINE_BUNDLE_ENTRYPOINT]
        != snapshot["bundle"]["entrypoint_sha256"]
    ):
        raise CheckoutBytesError("pin_tool_checkout_changed")
    return modified, fingerprints, snapshot["bundle"]


def _check_computation(manifest, computed, before, after):
    if before[1:] != after[1:]:
        raise CheckoutBytesError("pin_tool_checkout_changed")
    expected = {
        section: {
            name: before[1][(Path(base) / name).as_posix()]
            for name in manifest[section]
        }
        for section, base in CLOSURE_BASES.items()
    }
    expected["engine_bundle"] = before[2]
    if computed != expected:
        raise CheckoutBytesError("pin_tool_checkout_changed")


def check(root, manifest_path, publish_config, out):
    manifest, indent = load_manifest(manifest_path)
    before = _checkout_guard(root, manifest_path, manifest)
    computed = compute_all(root, manifest)
    after = _checkout_guard(root, manifest_path, manifest)
    _check_computation(manifest, computed, before, after)
    manifest_name = (
        Path(manifest_path).resolve().relative_to(Path(root).resolve()).as_posix()
    )
    if before[1][manifest_name] != _sha256(render(manifest, indent)):
        raise CheckoutBytesError("pin_tool_checkout_changed")
    stale = stale_entries(manifest, computed)
    config_path, display = _publish_config(root, publish_config)
    if config_path.is_file():
        actual = before[1][manifest_name]
        pinned = _publish_pin(config_path.read_bytes(), actual, display)
        if pinned != actual:
            stale.append(("publish_config", display, pinned, actual))
    for section, name, pinned, actual in stale:
        actual_text = "missing" if actual is None else _display(actual)
        print(f"{section} {name} {_display(pinned)} {actual_text}", file=out)
    return 1 if stale else 0


def write(root, manifest_path, publish_config, out):
    manifest, indent = load_manifest(manifest_path)
    before = render(manifest, indent)
    preflight = _checkout_guard(root, manifest_path, manifest, preflight=True)
    computed = compute_all(root, manifest)
    stale = stale_entries(manifest, computed)
    postflight = _checkout_guard(root, manifest_path, manifest, stale)
    _check_computation(manifest, computed, preflight, postflight)
    manifest_name = (
        Path(manifest_path).resolve().relative_to(Path(root).resolve()).as_posix()
    )
    if preflight[1][manifest_name] != _sha256(before):
        raise CheckoutBytesError("pin_tool_checkout_changed")
    modified = postflight[0]
    updated = apply_digests(manifest, computed)
    after = render(updated, indent)
    config_path, display = _publish_config(root, publish_config)
    config = config_path.read_bytes() if config_path.is_file() else None
    pinned = _publish_pin(config, _sha256(before), display) if config else None
    for name in modified:
        print(f"pin_tool_modified_repin {name}", file=out)
    if stale:
        Path(manifest_path).write_bytes(after)
        for section, name, old, new in stale:
            print(f"{section} {name} {old} {new}", file=out)
    else:
        print("no changes", file=out)
    after_sha = _sha256(after)
    print(f"manifest_sha256 before {_sha256(before)} after {after_sha}", file=out)
    if config is None:
        print(f"publish config absent {display}", file=out)
    elif pinned != after_sha:
        config_path.write_bytes(_replace_pin(config, pinned, after_sha))
        print(f"publish_config {display} {pinned} {after_sha}", file=out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="report stale entries")
    mode.add_argument("--write", action="store_true", help="repin digest fields")
    parser.add_argument("--root", default=str(ROOT), help="repository root")
    parser.add_argument(
        "--manifest", help=f"manifest path, default <root>/{MANIFEST_PATH}"
    )
    parser.add_argument(
        "--publish-config",
        default=PUBLISH_CONFIG_PATH,
        help="publish build config carrying the manifest sha256, skipped when absent",
    )
    args = parser.parse_args(argv)
    root = Path(args.root)
    manifest_path = Path(args.manifest) if args.manifest else root / MANIFEST_PATH
    try:
        if args.check:
            return check(root, manifest_path, args.publish_config, sys.stdout)
        return write(root, manifest_path, args.publish_config, sys.stdout)
    except (
        ManifestFormatError,
        ClosurePathError,
        ProtectedFieldError,
        PublishConfigError,
        EngineBundleError,
        CheckoutBytesError,
    ) as error:
        print(f"refused {type(error).__name__} {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
