"""Fixed read-only proof of storage already applied by the source-bound jobs release."""

import ast
import datetime as dt
import json
import re
from pathlib import Path

from core.schema import release_install as ri
from core.setup import durable_effects_check as de
from core.setup.release import (
    jobs_effects as je,
    jobs_only as jo,
    jobs_source as js,
    services_only as so,
)

KIND = "42_applied_jobs_storage_v1"
EFFECT_ID = "E-APPLIED-JOBS-STORAGE"
RECEIPT_FILE = "applied-jobs-storage-receipt.json"
FILE_ROLES = (
    "bindings",
    "packet_lock",
    "independent_review",
    "execution_receipt",
    "durable_manifest",
    "release_manifest",
    "schema_readback_receipt",
    "after_jobs_update",
    "jobs_update_log",
)
FIELDS = {
    "schema_version",
    "kind",
    "source",
    "jobs",
    "files",
    "installer_receipts",
    "storage_effects_sha256",
    "captured_utc",
    "deferred_effect_ids",
    "receipt_sha256",
}
REF = re.compile(r"intelligence_42_(?:core|agent)\.\w+")
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
STATEMENT = "Read-only proof of source-bound jobs-applied storage; no warehouse apply in services"


class Refused(de.Refusal, ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def receipt_hash(value):
    return so.fingerprint({k: v for k, v in value.items() if k != "receipt_sha256"})


def _read(path):
    path = Path(path)
    _plain_path(path)
    require(
        path.is_file() and not path.is_symlink(),
        "Missing or symlinked applied-storage evidence",
    )
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _plain_path(path):
    require(
        not any(
            p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
            for p in (Path(path), *Path(path).parents)
        ),
        "Symlinked or junction storage evidence path",
    )


def _inside(folder, path):
    _plain_path(folder)
    _plain_path(path)
    resolved = Path(path).resolve()
    require(
        resolved.is_relative_to(Path(folder).resolve()) and not Path(path).is_symlink(),
        "Storage evidence escapes its packet",
    )
    return resolved


def validate_receipt(value, commit, tree, *, now=None, max_age_minutes=None):
    require(
        isinstance(value, dict) and set(value) == FIELDS,
        "Unknown applied-storage receipt fields",
    )
    require(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["kind"] == KIND,
        "Unknown applied-storage receipt version",
    )
    require(
        value["source"] == {"commit": commit, "tree": tree}
        and COMMIT.fullmatch(commit)
        and COMMIT.fullmatch(tree),
        "Applied-storage receipt names foreign source",
    )
    require(
        isinstance(value["jobs"], dict)
        and set(value["jobs"]) == {"release_id", "image_digest", "packet_dir"},
        "Unknown jobs proof fields",
    )
    packet_dir = value["jobs"]["packet_dir"]
    require(
        isinstance(packet_dir, str)
        and Path(packet_dir).is_absolute()
        and str(Path(packet_dir).resolve()) == packet_dir,
        "Jobs packet path is not canonical absolute",
    )
    _plain_path(packet_dir)
    require(
        so.RELEASE_ID.fullmatch(value["jobs"]["release_id"])
        and value["jobs"]["release_id"][4:11] == commit[:7]
        and so.DIGEST.fullmatch(value["jobs"]["image_digest"]),
        "Foreign jobs release or image",
    )
    require(
        isinstance(value["files"], dict)
        and set(value["files"]) == set(FILE_ROLES)
        and all(
            isinstance(v, str) and SHA.fullmatch(v) for v in value["files"].values()
        ),
        "Incomplete jobs evidence hashes",
    )
    require(
        isinstance(value["installer_receipts"], dict)
        and bool(value["installer_receipts"])
        and all(
            re.fullmatch(r"runs/[A-Za-z0-9_-]+/warehouse-install-\d{4}\.json", p)
            and isinstance(h, str)
            and SHA.fullmatch(h)
            for p, h in value["installer_receipts"].items()
        ),
        "Unknown or incomplete installer evidence names",
    )
    require(
        isinstance(value["storage_effects_sha256"], str)
        and SHA.fullmatch(value["storage_effects_sha256"]),
        "Invalid storage inventory hash",
    )
    require(
        value["deferred_effect_ids"] == ["E-JOB-VIEWS"], "Deferred effect set differs"
    )
    require(
        value["receipt_sha256"] == receipt_hash(value), "Forged storage receipt hash"
    )
    try:
        captured = so.parse_utc(value["captured_utc"])
    except (ValueError, TypeError):
        raise Refused("Invalid storage capture clock") from None
    require(captured.tzinfo is not None, "Storage capture has no timezone")
    if now is not None:
        require(captured <= now, "Storage capture is from the future")
        require(
            type(max_age_minutes) is int
            and max_age_minutes > 0
            and now - captured <= dt.timedelta(minutes=max_age_minutes),
            "Storage receipt expired",
        )


def receipt_effect(release_id, sha, owner, order):
    return {
        "effect_id": EFFECT_ID,
        "kind": "receipt",
        "target": release_id,
        "change": "write_shape",
        "apply_order": order,
        "apply_phase": "before_candidate",
        "owner": dict(owner),
        "statement_ref": STATEMENT,
        "statement_sha256": de.sha_text(STATEMENT),
        "apply_kind": "code",
        "additive": True,
        "destructive": False,
        "authority_or_holds": False,
        "consumers": [
            {
                "component": "services storage proof",
                "image": "f42-web",
                "file": "core/setup/release/applied_storage.py",
                "line": 0,
                "access": "read",
            }
        ],
        "native_readback": {
            "kind": "helper_phase",
            "target": "BeforeCandidate",
            "expected_result_sha256": sha,
        },
        "compat": {
            k: {
                "verdict": "pending",
                "test": "actual source-bound jobs and old-reader proof required",
            }
            for k in de.COMPAT_KEYS
        },
        "forward_recovery": {
            "kind": "none_needed",
            "statement_ref": "",
            "statement_sha256": "",
            "note": "Services perform no warehouse writes. Existing recorded rollback remains unchanged.",
        },
        "evidence": {
            "applied_jobs_storage": {
                "receipt_file": RECEIPT_FILE,
                "receipt_sha256": sha,
            }
        },
    }


def object_closure(roots, definitions, source_queries=None):
    import sqlglot
    from sqlglot import exp

    result = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in result:
            continue
        require(name in definitions, "Required SQL dependency is unknown: " + name)
        result.add(name)
        body = (source_queries or {}).get(name, definitions[name].get("body", ""))
        if not body:
            continue
        try:
            query = sqlglot.parse_one(body, read="bigquery")
        except sqlglot.errors.ParseError:
            raise Refused("Unparsed SQL dependency body: " + name) from None
        dependencies = set()
        ctes = {cte.alias_or_name for cte in query.find_all(exp.CTE)}
        for table in query.find_all(exp.Table):
            if table.db.upper() == "ML" and isinstance(
                table.this, exp.GenerateEmbedding
            ):
                continue
            if (
                not table.db
                and isinstance(table.this, exp.Anonymous)
                and "." in table.this.name
            ):
                parts = table.this.name.lower().split(".")
                require(
                    len(parts) in (2, 3)
                    and (len(parts) == 2 or parts[0] == so.PROJECT)
                    and parts[-2] in ("intelligence_42_core", "intelligence_42_agent"),
                    "Unknown quoted SQL function dependency",
                )
                dependencies.add(".".join(parts[-2:]))
                continue
            require(
                not table.catalog or table.catalog == so.PROJECT,
                "Foreign SQL project dependency",
            )
            require(
                bool(table.db) or table.name in ctes,
                "Unresolved unqualified SQL dependency: " + table.sql(),
            )
            require(
                not table.db
                or table.db in ("intelligence_42_core", "intelligence_42_agent"),
                "Foreign SQL object dependency: " + table.sql(),
            )
            if table.db in ("intelligence_42_core", "intelligence_42_agent"):
                item = (
                    table.this.name
                    if isinstance(table.this, exp.Anonymous)
                    else table.name
                )
                require(bool(item), "Unresolved SQL object identifier: " + name)
                dependencies.add(table.db + "." + item.lower())
        for function in query.find_all(exp.Dot):
            if isinstance(function.expression, exp.Anonymous):
                namespace = function.this.sql()
                if (
                    namespace.upper() == "ML"
                    and function.expression.name.upper() == "DISTANCE"
                ):
                    continue
                require(
                    namespace in ("intelligence_42_core", "intelligence_42_agent"),
                    "Unknown qualified SQL function dependency: " + function.sql(),
                )
                dependencies.add(namespace + "." + function.expression.name.lower())
        pending.extend(dependencies - result)
    return result


def _object_literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(value.value)
            elif (
                isinstance(value, ast.FormattedValue)
                and isinstance(value.value, ast.Name)
                and value.value.id in ("CORE", "AGENT")
            ):
                parts.append("intelligence_42_" + value.value.id.lower())
            else:
                return None
        return "".join(parts)
    return None


def _guarded_absence(parsed, target):
    occurrences = [
        n
        for n in ast.walk(parsed)
        if isinstance(n, (ast.JoinedStr, ast.Constant)) and _object_literal(n) == target
    ]
    if not occurrences:
        return False
    guards = {}
    covered = set()
    for function in ast.walk(parsed):
        if not isinstance(function, ast.FunctionDef):
            continue
        body = [
            n
            for n in function.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
        ]
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            continue
        check = body[0].value
        if (
            not isinstance(check, ast.Compare)
            or len(check.ops) != 1
            or not isinstance(check.ops[0], ast.In)
            or _object_literal(check.left) != target
        ):
            continue
        right = check.comparators[0]
        if not (
            isinstance(right, ast.Subscript)
            and isinstance(right.slice, ast.Constant)
            and right.slice.value == "objects"
            and isinstance(right.value, ast.Call)
            and isinstance(right.value.func, ast.Attribute)
            and isinstance(right.value.func.value, ast.Name)
            and right.value.func.value.id == "self"
            and right.value.func.attr == "_catalog"
            and not right.value.args
            and not right.value.keywords
        ):
            continue
        guards[function.name] = check.left
        covered.add(check.left)
    for branch in ast.walk(parsed):
        if not isinstance(branch, ast.IfExp):
            continue
        test = branch.test
        if not (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Attribute)
            and isinstance(test.func.value, ast.Name)
            and test.func.value.id == "self"
            and test.func.attr in guards
            and not test.args
            and not test.keywords
            and isinstance(branch.orelse, ast.List)
            and not branch.orelse.elts
        ):
            continue
        covered.update(n for n in ast.walk(branch.body) if n in occurrences)
    return set(occurrences) <= covered


def _missing_import_guarded(parsed, node):
    for block in ast.walk(parsed):
        if not isinstance(block, ast.Try) or not any(
            child is node for statement in block.body for child in ast.walk(statement)
        ):
            continue
        for handler in block.handlers:
            types = (
                handler.type.elts
                if isinstance(handler.type, ast.Tuple)
                else [handler.type]
            )
            if not any(
                isinstance(t, ast.Name) and t.id == "ImportError" for t in types
            ):
                continue
            last = handler.body[-1]
            if isinstance(last, (ast.Continue, ast.Return, ast.Raise)) or (
                isinstance(last, ast.Expr)
                and isinstance(last.value, ast.Call)
                and isinstance(last.value.func, ast.Name)
                and last.value.func.id == "_refused"
            ):
                return True
    return False


def _identifier_values(parsed, value, context, files=None, path=None, seen=()):
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return {value.value}
    if isinstance(value, ast.JoinedStr):
        forms = {""}
        for fragment in value.values:
            choices = _identifier_values(
                parsed,
                fragment.value
                if isinstance(fragment, ast.FormattedValue)
                else fragment,
                context,
                files,
                path,
                seen,
            )
            if choices is None:
                return None
            forms = {prefix + suffix for prefix in forms for suffix in choices}
        return forms
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        left = _identifier_values(parsed, value.left, context, files, path, seen)
        right = _identifier_values(parsed, value.right, context, files, path, seen)
        return (
            {a + b for a in left for b in right}
            if left is not None and right is not None
            else None
        )
    owners = [
        n
        for n in ast.walk(parsed)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(child is context for child in ast.walk(n))
    ]
    owner = min(owners, key=lambda n: len(list(ast.walk(n)))) if owners else None
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and not value.args
        and not value.keywords
    ):
        funcs = [
            n
            for n in parsed.body
            if isinstance(n, ast.FunctionDef) and n.name == value.func.id
        ]
        if (
            len(funcs) == 1
            and len(funcs[0].body) == 1
            and isinstance(funcs[0].body[0], ast.Return)
        ):
            expected = ast.parse(
                'os.environ.get("F42_PROJECT", "ogilvy-trends-v2")', mode="eval"
            ).body
            if ast.dump(funcs[0].body[0].value) == ast.dump(expected):
                return {so.PROJECT}
        return None
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "self"
        and value.attr == "project"
    ):
        cls = next(
            (
                n
                for n in ast.walk(parsed)
                if isinstance(n, ast.ClassDef) and owner in n.body
            ),
            None,
        )
        if cls is None:
            return None
        writes = [
            n
            for n in ast.walk(cls)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Attribute)
                and isinstance(t.value, ast.Name)
                and t.value.id == "self"
                and t.attr == "project"
                for t in n.targets
            )
        ]
        constructor = next(
            (
                n
                for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == "__init__"
            ),
            None,
        )
        if len(writes) != 1 or constructor is None or writes[0] not in constructor.body:
            return None
        expression = writes[0].value
        if isinstance(expression, ast.BoolOp) and isinstance(expression.op, ast.Or):
            expected = ast.parse(
                'project or os.environ.get("F42_PROJECT") or "ogilvy-trends-v2"',
                mode="eval",
            ).body
            return {so.PROJECT} if ast.dump(expression) == ast.dump(expected) else None
        if isinstance(expression, ast.Name) and expression.id == "project":
            args = constructor.args.args
            defaults = dict(
                zip(
                    [a.arg for a in args[-len(constructor.args.defaults) :]],
                    constructor.args.defaults,
                )
            )
            default = defaults.get("project")
            return (
                _identifier_values(parsed, default, constructor, files, path, seen)
                if default is not None
                else None
            )
        return None
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and files is not None
    ):
        alias = value.value.id
        if owner is not None and (
            any(
                a.arg == alias
                for a in (
                    *owner.args.posonlyargs,
                    *owner.args.args,
                    *owner.args.kwonlyargs,
                )
            )
            or any(
                isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == alias for t in n.targets)
                for n in ast.walk(owner)
            )
        ):
            return None
        imports = [
            n
            for n in parsed.body
            if isinstance(n, ast.ImportFrom)
            and n.module
            and n.module.startswith("core.")
        ]
        modules = [
            n.module + "." + a.name
            for n in imports
            for a in n.names
            if (a.asname or a.name) == alias
        ]
        modules += [
            a.name
            for n in parsed.body
            if isinstance(n, ast.Import)
            for a in n.names
            if a.asname == alias and a.name.startswith("core.")
        ]
        if len(modules) != 1:
            return None
        target = modules[0].replace(".", "/") + ".py"
        if target not in files:
            return None
        imported = ast.parse(files[target])
        return _identifier_values(
            imported, ast.Name(id=value.attr), imported, files, target, seen
        )
    name = (
        value.id
        if isinstance(value, ast.Name)
        else value.value.id
        if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name)
        else None
    )
    if name is None:
        return None
    if owner is not None and any(
        a.arg == name
        for a in (*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs)
    ):
        if files is None or path is None or owner not in parsed.body:
            return None
        position = next(i for i, a in enumerate(owner.args.args) if a.arg == name)
        key = (path, owner.name, name)
        if key in seen:
            return None
        arguments = []
        module = path.removesuffix(".py").replace("/", ".")
        for caller_path, text in files.items():
            if not caller_path.endswith(".py") or de.is_test_path(caller_path):
                continue
            caller = parsed if caller_path == path else ast.parse(text)
            functions = {owner.name} if caller_path == path else set()
            aliases = set()
            safe_references = set()
            for node in caller.body:
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        if node.module == module and alias.name == owner.name:
                            functions.add(alias.asname or alias.name)
                        if node.module + "." + alias.name == module:
                            aliases.add(alias.asname or alias.name)
                if isinstance(node, ast.Import):
                    aliases.update(
                        alias.asname
                        for alias in node.names
                        if alias.name == module and alias.asname
                    )
            for node in ast.walk(caller):
                called = isinstance(node, ast.Call) and (
                    (isinstance(node.func, ast.Name) and node.func.id in functions)
                    or (
                        isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in aliases
                        and node.func.attr == owner.name
                    )
                )
                if called:
                    safe_references.add(node.func)
                    argument = (
                        node.args[position]
                        if len(node.args) > position
                        else next(
                            (k.value for k in node.keywords if k.arg == name), None
                        )
                    )
                    if argument is None:
                        return None
                    arguments.append((caller, argument, node, caller_path))
            if caller_path == path:
                for loop in ast.walk(caller):
                    if (
                        not isinstance(loop, ast.For)
                        or not isinstance(loop.target, ast.Tuple)
                        or not isinstance(loop.iter, (ast.Tuple, ast.List))
                    ):
                        continue
                    for index, target in enumerate(loop.target.elts):
                        if not isinstance(target, ast.Name):
                            continue
                        rows = loop.iter.elts
                        if not rows or not all(
                            isinstance(row, ast.Tuple)
                            and len(row.elts) > index
                            and isinstance(row.elts[index], ast.Name)
                            for row in rows
                        ):
                            continue
                        if not any(row.elts[index].id == owner.name for row in rows):
                            continue
                        safe_references.update(
                            row.elts[index]
                            for row in rows
                            if row.elts[index].id == owner.name
                        )
                        for call in ast.walk(loop):
                            if (
                                isinstance(call, ast.Call)
                                and isinstance(call.func, ast.Name)
                                and call.func.id == target.id
                            ):
                                if len(call.args) <= position:
                                    return None
                                arguments.append(
                                    (caller, call.args[position], call, caller_path)
                                )
            for reference in ast.walk(caller):
                names_function = (
                    isinstance(reference, ast.Name)
                    and reference.id in functions
                    or (
                        isinstance(reference, ast.Attribute)
                        and isinstance(reference.value, ast.Name)
                        and reference.value.id in aliases
                        and reference.attr == owner.name
                    )
                )
                if names_function and reference not in safe_references:
                    return None
        values = [
            _identifier_values(p, argument, call, files, caller_path, (*seen, key))
            for p, argument, call, caller_path in arguments
        ]
        return (
            set().union(*values)
            if values and all(v is not None for v in values)
            else None
        )
    local = (
        [
            n
            for n in ast.walk(owner)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
        ]
        if owner
        else []
    )
    if local:
        return None
    bindings = []
    for assignment in parsed.body:
        if not isinstance(assignment, ast.Assign):
            continue
        for target in assignment.targets:
            if isinstance(target, ast.Name) and target.id == name:
                bindings.append(assignment.value)
            elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(
                assignment.value, (ast.Tuple, ast.List)
            ):
                bindings.extend(
                    v
                    for t, v in zip(target.elts, assignment.value.elts)
                    if isinstance(t, ast.Name) and t.id == name
                )
    if len(bindings) == 1:
        if any(
            isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Subscript)
                and isinstance(t.value, ast.Name)
                and t.value.id == name
                for t in n.targets
            )
            for n in ast.walk(parsed)
        ):
            return None
        try:
            fixed = ast.literal_eval(bindings[0])
        except (ValueError, TypeError):
            key = (path, name)
            return (
                _identifier_values(
                    parsed, bindings[0], parsed, files, path, (*seen, key)
                )
                if key not in seen
                else None
            )
        values = (
            list(fixed.values())
            if isinstance(value, ast.Subscript) and isinstance(fixed, dict)
            else [fixed]
        )
        return (
            set(values)
            if values
            and all(isinstance(v, str) and re.fullmatch(r"[\w.-]+", v) for v in values)
            else None
        )
    loops = [
        n
        for n in ast.walk(owner or parsed)
        if isinstance(n, ast.For)
        and isinstance(n.target, ast.Name)
        and n.target.id == name
        and any(child is context for child in ast.walk(n))
    ]
    if len(loops) == 1 and isinstance(loops[0].iter, (ast.Tuple, ast.List)):
        values = [
            _identifier_values(parsed, n, context, files, path, seen)
            for n in loops[0].iter.elts
        ]
        return (
            set().union(*values)
            if values and all(v is not None for v in values)
            else None
        )
    return None


def compare_native(expected, observed):
    require(set(expected) <= set(observed), "Required native storage object absent")
    for name, wanted in expected.items():
        require(
            observed[name] == wanted,
            "Native storage definition/type/signature drift: " + name,
        )


def storage_inventory(durable):
    return [
        {
            k: e[k]
            for k in (
                "effect_id",
                "target",
                "statement_sha256",
                "apply_order",
                "native_readback",
            )
        }
        for e in durable["effects"]
        if e["apply_kind"] in ("schema", "warehouse_install")
    ]


def _latest(folder, pattern):
    files = sorted(Path(folder).glob(pattern))
    require(bool(files), "Missing completed jobs event")
    return files[-1]


def inspect_jobs(repo, commit, tree, packet_dir):
    root = Path(packet_dir).resolve()
    bound = _read(root / "live-bindings.json")
    jo.validate_jobs_bindings(bound)
    require(
        bound["target"] == commit
        and bound["tree"] == tree
        and Path(bound["releaseDir"]).resolve() == root,
        "Jobs packet is not this source and directory",
    )
    paths = {
        "bindings": root / "live-bindings.json",
        "packet_lock": root.parent
        / "locks/jobs"
        / ("PACKET-LOCK-" + bound["release_id"] + ".json"),
        "independent_review": root / "independent-review.json",
        "execution_receipt": root / "execution-receipt.json",
        "durable_manifest": _inside(root, bound["durableManifestPath"]),
        "release_manifest": root / "release-manifest.json",
        "schema_readback_receipt": _inside(root, bound["schemaReadbackReceiptPath"]),
        "after_jobs_update": _latest(root / "readbacks", "AfterJobsUpdate-*.json"),
        "jobs_update_log": _latest(root, "runs/*/jobs-update-*.json"),
    }
    values = {key: _read(path) for key, path in paths.items()}
    from core.setup.release import lock

    require(
        not lock.lock_problems(repo, values["packet_lock"]), "Jobs source lock differs"
    )
    from core.setup.release import packet

    locked = packet.jobs_bound_file_paths(bound, paths["bindings"])
    require(
        values["packet_lock"].get("release_id") == bound["release_id"]
        and all(
            values["packet_lock"].get("bound_files", {}).get(k)
            == so.sha_bytes(p.read_bytes())
            for k, p in locked.items()
        ),
        "Jobs packet bound evidence changed",
    )
    review = values["independent_review"]
    require(
        review.get("verdict") == "ACCEPT"
        and review.get("target") == commit
        and review.get("lock_sha256")
        == so.sha_bytes(paths["packet_lock"].read_bytes()),
        "Jobs independent review does not bind source",
    )
    receipt = values["execution_receipt"]
    require(
        receipt.get("mode") == "jobs"
        and receipt.get("target") == commit
        and receipt.get("release_id") == bound["release_id"]
        and "JobsUpdate" in receipt.get("declared_steps", [])
        and all(
            receipt.get(k) is True
            for k in (
                "later_explicit_user_instruction",
                "quiet_window_confirmed",
                "no_new_manual_starts_until_execution_ends",
            )
        ),
        "Jobs execution authority is incomplete",
    )
    durable = jo.load_durable(bound)
    require(
        not de.validate_manifest(durable)
        and durable["source"] == {"commit": commit, "tree": tree},
        "Foreign jobs durable manifest",
    )
    files = de.git_tree_files(commit, cwd=repo)
    ri.validate_effects(durable, ri.statements(files))
    source = dict(files)
    old = de.git_tree_files(je.A80, cwd=repo)
    require(
        not de.check_schema_effects(
            durable,
            [source["core/schema/" + p] for p in js.sql_file_names(files)],
            [dict(old)["core/schema/" + p] for p in js.sql_file_names(old)],
        ),
        "Incomplete schema effect inventory",
    )
    jo.check_schema_receipt(bound)
    after = values["after_jobs_update"]
    require(
        after.get("phase") == "AfterJobsUpdate"
        and after.get("mode") == "jobs"
        and after.get("release_id") == bound["release_id"],
        "Foreign jobs completion event",
    )
    update = values["jobs_update_log"]
    require(
        update.get("action") == "update"
        and update.get("complete") is True
        and update.get("stopped") is None
        and set(update.get("updated", []) + update.get("skipped_already_new", []))
        == set(so.JOB_NAMES),
        "Jobs update incomplete",
    )
    digest = jo.frozen_digest(root)
    runs = sorted(
        p
        for p in (root / "runs").glob("*")
        if p.is_dir() and any(p.glob("warehouse-install-*.json"))
    )
    require(bool(runs), "No original installer run")
    ri.require_receipts(durable, runs[-1], bound["release_id"])
    hashes = {
        str(p.relative_to(root)).replace("\\", "/"): so.sha_bytes(p.read_bytes())
        for p in sorted(runs[-1].glob("warehouse-install-*.json"))
    }
    return bound, durable, paths, hashes, digest


def effect_reference(manifest):
    effects = [e for e in manifest["effects"] if e.get("effect_id") == EFFECT_ID]
    if not effects:
        return None
    require(len(effects) == 1, "Repeated applied-storage effect")
    effect = effects[0]
    require(
        (effect["kind"], effect["apply_kind"], effect["change"], effect["apply_phase"])
        == ("receipt", "code", "write_shape", "before_candidate")
        and effect["statement_ref"] == STATEMENT
        and effect["statement_sha256"] == de.sha_text(STATEMENT),
        "Unknown applied-storage effect shape",
    )
    ref = effect.get("evidence", {}).get("applied_jobs_storage")
    require(
        effect["evidence"] == {"applied_jobs_storage": ref}
        and isinstance(ref, dict)
        and set(ref) == {"receipt_file", "receipt_sha256"}
        and ref["receipt_file"] == RECEIPT_FILE
        and SHA.fullmatch(str(ref["receipt_sha256"])),
        "Unknown applied-storage evidence reference",
    )
    return ref


def receipt_path(manifest, packet_dir):
    ref = effect_reference(manifest)
    if ref is None:
        return None
    path = _inside(packet_dir, Path(packet_dir) / RECEIPT_FILE)
    require(
        path.is_file() and so.sha_bytes(path.read_bytes()) == ref["receipt_sha256"],
        "Locked applied-storage receipt differs",
    )
    return path


def dependency_inventory(repo, commit):
    """Closed first-party caller/reference graph; unresolved identifiers are refusals."""
    files = dict(de.git_tree_files(commit, cwd=repo))
    roots = ["core/api/app.py", "core/api/agent_app.py"]
    pending, reached, references, catalog_names, parsed_callers = (
        roots[:],
        set(),
        set(),
        set(),
        [],
    )
    while pending:
        path = pending.pop()
        if path in reached:
            continue
        require(path in files, "Unknown first-party service entry/import: " + path)
        reached.add(path)
        text = files[path]
        qualified = text.replace("{CORE}", "intelligence_42_core").replace(
            "{AGENT}", "intelligence_42_agent"
        )
        references.update(
            name
            for name in REF.findall(qualified)
            if not name.endswith(".INFORMATION_SCHEMA")
        )
        parsed = ast.parse(text)
        parsed_callers.append(parsed)
        parents = {
            child: parent
            for parent in ast.walk(parsed)
            for child in ast.iter_child_nodes(parent)
        }
        for literal in ast.walk(parsed):
            if not isinstance(literal, ast.JoinedStr):
                continue
            expressions = []
            fragments = []
            for value in literal.values:
                if isinstance(value, ast.Constant):
                    fragments.append(str(value.value))
                else:
                    fragments.append("{F" + str(len(expressions)) + "}")
                    expressions.append(value.value)
            query_template = "".join(fragments)
            for lookup in re.finditer(
                r"\b(?:FROM|JOIN|MODEL)\s+`?([\w{}.-]+\.[\w{}.-]+)",
                query_template,
                re.I,
            ):
                forms = {lookup[1]}
                for slot in re.findall(r"\{F(\d+)\}", lookup[1]):
                    value = expressions[int(slot)]
                    fixed = _identifier_values(parsed, value, literal, files, path)
                    require(
                        fixed is not None,
                        "Unresolved dynamic SQL object identifier: "
                        + path
                        + ":"
                        + ast.unparse(value),
                    )
                    forms = {
                        form.replace("{F" + slot + "}", item)
                        for form in forms
                        for item in fixed
                    }
                for form in forms:
                    parts = form.split(".")
                    for index, part in enumerate(parts[:-1]):
                        if (
                            part in ("intelligence_42_core", "intelligence_42_agent")
                            and parts[index + 1] != "INFORMATION_SCHEMA"
                        ):
                            references.add(part + "." + parts[index + 1])
        for literal in ast.walk(parsed):
            if isinstance(parents.get(literal), (ast.JoinedStr, ast.BinOp)):
                continue
            value = _object_literal(literal)
            if (
                value
                and re.match(r"\s*(SELECT|WITH)\b", value, re.I)
                and "intelligence_42_" in value
            ):
                import sqlglot
                from sqlglot import exp

                try:
                    query = sqlglot.parse_one(value, read="bigquery")
                except sqlglot.errors.ParseError:
                    raise Refused("Unparsed static caller SQL: " + path) from None
                for table in query.find_all(exp.Table):
                    if table.db:
                        require(
                            table.db
                            in ("intelligence_42_core", "intelligence_42_agent")
                            and (not table.catalog or table.catalog == so.PROJECT),
                            "Foreign caller SQL dependency",
                        )
                        name = (
                            table.this.name
                            if isinstance(table.this, exp.Anonymous)
                            else table.name
                        )
                        require(bool(name), "Unresolved caller SQL dependency")
                        references.add(table.db + "." + name.lower())
                for function in query.find_all(exp.Dot):
                    if isinstance(function.expression, exp.Anonymous):
                        namespace = function.this.sql()
                        require(
                            namespace
                            in ("intelligence_42_core", "intelligence_42_agent"),
                            "Foreign caller SQL function",
                        )
                        references.add(
                            namespace + "." + function.expression.name.lower()
                        )
        import_aliases = {
            a.asname or a.name
            for n in ast.walk(parsed)
            if isinstance(n, ast.ImportFrom) and n.module == "importlib"
            for a in n.names
            if a.name == "import_module"
        }
        for node in ast.walk(parsed):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_find"
            ):
                require(bool(node.args), "Unresolved catalog lookup")
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    catalog_names.add(arg.value)
                elif isinstance(arg, ast.Name):
                    containers = [
                        item
                        for item in ast.walk(parsed)
                        if isinstance(
                            item, (ast.GeneratorExp, ast.ListComp, ast.SetComp)
                        )
                        and any(child is node for child in ast.walk(item.elt))
                    ]
                    loops = [
                        loop
                        for item in containers
                        for loop in item.generators
                        if isinstance(loop.target, ast.Name)
                        and loop.target.id == arg.id
                    ]
                    values = []
                    for item in loops:
                        try:
                            value = ast.literal_eval(item.iter)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(value, (tuple, list)) and all(
                            isinstance(v, str) for v in value
                        ):
                            values.extend(value)
                    require(bool(values), "Unresolved dynamic catalog lookup")
                    catalog_names.update(values)
                else:
                    raise Refused("Unresolved dynamic catalog lookup")
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = path.rsplit("/", 1)[0].split("/")
                if node.level:
                    prefix = prefix[: len(prefix) - node.level + 1]
                    module = ".".join(prefix + ([node.module] if node.module else []))
                else:
                    module = node.module or ""
                modules = [module] + [module + "." + alias.name for alias in node.names]
            else:
                modules = []
            for module in modules:
                if not module.startswith("core."):
                    continue
                candidates = (
                    module.replace(".", "/") + ".py",
                    module.replace(".", "/") + "/__init__.py",
                )
                found = next((p for p in candidates if p in files), None)
                if found:
                    pending.append(found)
                elif module == modules[0]:
                    namespace = module.replace(".", "/") + "/"
                    require(
                        any(p.startswith(namespace) for p in files)
                        or _missing_import_guarded(parsed, node),
                        "Missing static first-party import: " + module,
                    )
            if isinstance(node, ast.Call) and (
                (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                )
                or (
                    isinstance(node.func, ast.Name)
                    and node.func.id in import_aliases | {"__import__"}
                )
            ):
                values = []
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    values = [node.args[0].value]
                elif node.args and isinstance(node.args[0], ast.Name):
                    variable = node.args[0].id
                    module_assignments = [
                        item for item in parsed.body if isinstance(item, ast.Assign)
                    ]
                    local_loops = [
                        item
                        for item in ast.walk(parsed)
                        if isinstance(item, ast.For)
                        and any(
                            child is node
                            for statement in item.body
                            for child in ast.walk(statement)
                        )
                    ]
                    for item in module_assignments + local_loops:
                        if isinstance(item, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == variable
                            for t in item.targets
                        ):
                            try:
                                value = ast.literal_eval(item.value)
                            except (ValueError, TypeError):
                                continue
                            if isinstance(value, str):
                                values.append(value)
                        if (
                            isinstance(item, ast.For)
                            and isinstance(item.target, ast.Name)
                            and item.target.id == variable
                        ):
                            try:
                                value = ast.literal_eval(item.iter)
                            except (ValueError, TypeError):
                                continue
                            if isinstance(value, (tuple, list)) and all(
                                isinstance(v, str) for v in value
                            ):
                                values.extend(value)
                require(bool(values), "Unresolved dynamic service import")
                for value in values:
                    if value.startswith("core."):
                        candidates = (
                            value.replace(".", "/") + ".py",
                            value.replace(".", "/") + "/__init__.py",
                        )
                        target = next((p for p in candidates if p in files), None)
                        if target:
                            pending.append(target)
                        else:
                            require(
                                any(
                                    p.startswith(value.replace(".", "/") + "/")
                                    for p in files
                                )
                                or _missing_import_guarded(parsed, node),
                                "Missing dynamic first-party import: " + value,
                            )
    from core.detect import sqlrun
    from core.schema import apply
    import sqlglot
    from sqlglot import exp

    renderer_root = Path(sqlrun.__file__).resolve().parents[2]
    renderer_inputs = (
        "core/detect/sqlrun.py",
        "core/schema/apply.py",
        "core/config/hubs.yaml",
        "core/detect/sensitive.yaml",
        "core/brief/political.yaml",
    )
    renderer_inputs += tuple(
        p for p in files if p.startswith("core/detect/sql/") and p.endswith(".sql")
    )
    for path in renderer_inputs:
        require(
            path in files
            and (renderer_root / path).read_text(encoding="utf-8") == files[path],
            "Dependency renderer input is not the bound source: " + path,
        )
    definitions, source_queries = {}, {}
    for sql in (
        sqlrun.statements()
        + sqlrun.agent_statements()
        + sqlrun.spread_statements()
        + sqlrun.news_statements()
        + sqlrun.locality_view_statements()
        + [s["statement"] for s in ri.statements(list(files.items()))]
    ):
        definition = ri.expected_definition(sql)
        definitions[definition["target"]] = definition
        source_queries[definition["target"]] = re.split(
            r"\bAS\s+", sql, maxsplit=1, flags=re.I
        )[1]
    for path, text in files.items():
        if not path.endswith(".sql") or de.is_test_path(path):
            continue
        clean = re.sub(
            r"'(?:''|\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|/\*.*?\*/|--[^\n]*",
            lambda m: "" if m.group().startswith(("/*", "--")) else m.group(),
            text,
            flags=re.S,
        )
        for piece in sqlrun.split(sqlrun.render(clean)):
            statement = sqlrun._strip_leading_comments(piece)
            if re.match(
                r"CREATE(?: OR REPLACE)? (VIEW|TABLE FUNCTION)\b", statement, re.I
            ):
                if re.search(r"\{\w+\}", statement):
                    continue
                definition = ri.expected_definition(statement)
                definitions.setdefault(definition["target"], definition)
                source_queries.setdefault(
                    definition["target"],
                    re.split(r"\bAS\s+", statement, maxsplit=1, flags=re.I)[1],
                )
            if re.match(r"CREATE MODEL", statement, re.I):
                names = REF.findall(statement)
                endpoint = re.search(r"endpoint\s*=\s*'([^']+)'", statement, re.I)
                connection = re.search(
                    r"REMOTE WITH CONNECTION\s+`([^`]+)`", statement, re.I
                )
                require(
                    bool(names and endpoint and connection),
                    "Unknown remote model source",
                )
                if names and endpoint and connection:
                    definitions[names[0]] = {
                        "kind": "model",
                        "target": names[0],
                        "body": "",
                        "endpoint": endpoint[1],
                        "connection": connection[1],
                    }
    for path, text in files.items():
        if not path.startswith("core/schema/") or not path.endswith(".sql"):
            continue
        for statement in apply.split_statements(text):
            if apply.kind(statement) != "table":
                continue
            parsed = sqlglot.parse_one(statement, read="bigquery")
            schema = parsed.this
            if not isinstance(schema, exp.Schema):
                raise Refused("Unparsed source schema")
            target = ".".join(apply.statement_name(statement).split(".")[-2:])
            columns = []
            for column in schema.expressions:
                if not isinstance(column, exp.ColumnDef):
                    continue
                nullable = not any(
                    isinstance(c.args.get("kind"), exp.NotNullColumnConstraint)
                    for c in column.args.get("constraints", [])
                )
                columns.append(
                    {
                        "column_name": column.name,
                        "data_type": ri.normalized_sql(
                            column.args["kind"].sql(dialect="bigquery")
                        ),
                        "is_nullable": "YES" if nullable else "NO",
                    }
                )
            definitions[target] = {
                "kind": "table",
                "target": target,
                "body": "",
                "columns": sorted(columns, key=lambda c: c["column_name"]),
            }
    for statement in js.schema_statements(list(files.items())):
        if de.statement_kind(statement) == "alter":
            match = re.search(
                r"`[^`]+\.(intelligence_42_\w+)\.(\w+)`\s+ADD COLUMN IF NOT EXISTS\s+`?(\w+)`?\s+(\w+)",
                statement,
                re.I,
            )
            require(match is not None, "Unknown schema ALTER dependency")
            target = match[1] + "." + match[2]
            if target in definitions:
                columns = definitions[target]["columns"]
                if not any(c["column_name"] == match[3] for c in columns):
                    columns.append(
                        {
                            "column_name": match[3],
                            "data_type": match[4].upper(),
                            "is_nullable": "YES",
                        }
                    )
                    columns.sort(key=lambda c: c["column_name"])
    for name in catalog_names:
        matches = {target for target in definitions if target.split(".")[-1] == name}
        require(bool(matches), "Required catalog dependency is unknown: " + name)
        references.update(matches)
    for target in references - set(definitions):
        mentioning = [
            p
            for p in parsed_callers
            if any(_object_literal(n) == target for n in ast.walk(p))
        ]
        if mentioning and all(_guarded_absence(p, target) for p in mentioning):
            definitions[target] = {"kind": "absent", "target": target, "body": ""}
    required = object_closure(references, definitions, source_queries)
    return {name: definitions[name] for name in sorted(required)}


class Native:
    def __init__(self, bound, bq=None, reader=None):
        from core.setup.release.bound_readback import GcloudReader

        self.bq = bq or de.BigQuery()
        self.reader = reader or GcloudReader(bound["readTimeoutSeconds"])

    def verify_jobs(self, bound, digest):
        baseline = jo.load_baseline_j(bound)
        for name in so.JOB_NAMES:
            require(
                so.job_view(self.reader.job(name))
                == jo.with_image(baseline["jobs"][name], digest),
                "Current jobs image/template differs from completed release",
            )
        require(
            self.reader.registry_digest(jo.image_tag(bound)) == digest,
            "Jobs registry source tag drift",
        )
        from core.setup.release import jobs_freeze

        manifest = _read(Path(bound["releaseDir"]) / "release-manifest.json")
        build_id = manifest["image"]["build_id"]
        build = self.reader.build(build_id)
        tag, _ = jobs_freeze.expected_tag(bound)
        jobs_freeze.check_build_record(bound, build, build_id, jo.image_tag(bound), tag)
        require(
            jobs_freeze.build_digest(build, jo.image_tag(bound)) == digest,
            "Native jobs build differs from source-bound image",
        )

    def definition(self, wanted):
        target = wanted["target"]
        require(
            re.fullmatch(r"intelligence_42_(core|agent)\.\w+", target),
            "Unregistered native target",
        )
        if wanted["kind"] == "absent":
            from google.api_core.exceptions import NotFound

            try:
                self.bq._client.get_table(so.PROJECT + "." + target)
            except NotFound:
                return dict(wanted)
            raise Refused("Guarded unknown dependency became present: " + target)
        if wanted["kind"] == "table":
            dataset, name = target.split(".")
            sql = f"SELECT column_name, data_type, is_nullable FROM `{so.PROJECT}.{dataset}.INFORMATION_SCHEMA.COLUMNS` WHERE table_name = '{name}' ORDER BY column_name"
            cap = 10 * 1024 * 1024
            require(self.bq.dry_run(sql) <= cap, "Schema metadata cap exceeded")
            rows = self.bq.query(sql, None, cap)
            columns = [
                {
                    "column_name": r["column_name"],
                    "data_type": ri.normalized_sql(r["data_type"]),
                    "is_nullable": r["is_nullable"],
                }
                for r in rows
            ]
            return {"kind": "table", "target": target, "body": "", "columns": columns}
        typed = wanted["kind"] + ":" + target
        if wanted["kind"] == "model":
            api = self.bq._client.get_model(so.PROJECT + "." + target).to_api_repr()
            dataset, name = target.split(".")
            require(
                api.get("modelReference")
                == {"projectId": so.PROJECT, "datasetId": dataset, "modelId": name},
                "Foreign native remote model",
            )
            info = api.get("remoteModelInfo") or {}
            connection = info.get("connection", "")
            match = re.fullmatch(
                r"projects/([^/]+)/locations/([^/]+)/connections/([^/]+)", connection
            )
            require(
                match is not None
                or re.fullmatch(r"[\w-]+\.[\w-]+\.[\w-]+", connection),
                "Unknown native remote model connection",
            )
            endpoint = info.get("endpoint", "")
            prefix = (
                "projects/"
                + so.PROJECT
                + "/locations/"
                + so.REGION
                + "/publishers/google/models/"
            )
            require(
                endpoint.startswith(prefix)
                and re.fullmatch(r"[\w.-]+", endpoint[len(prefix) :]),
                "Unknown native remote model endpoint",
            )
            return {
                "kind": "model",
                "target": target,
                "body": "",
                "endpoint": endpoint[len(prefix) :],
                "connection": ".".join(match.groups()) if match else connection,
            }
        if target in ri.TARGETS:
            return self.bq.definition(typed)
        require(wanted["kind"] in ("view", "routine"), "Unknown dependency type")
        client = self.bq._client
        full = so.PROJECT + "." + target
        obj = (
            client.get_table(full)
            if wanted["kind"] == "view"
            else client.get_routine(full)
        )
        return ri.definition_from_api(wanted["kind"], target, obj.to_api_repr())


def _native_checks(repo, commit, bound, durable, digest, native, inventory=None):
    native.verify_jobs(bound, digest)
    wanted = dependency_inventory(repo, commit) if inventory is None else inventory
    specs = ri.statements(de.git_tree_files(commit, cwd=repo))
    for spec in specs:
        wanted[spec["target"]] = spec["definition"]
    observed = {name: native.definition(value) for name, value in wanted.items()}
    compare_native(wanted, observed)


def produce(
    repo,
    commit,
    tree,
    packet_dir,
    output,
    *,
    now=None,
    native_factory=None,
    inventory=None,
):
    now = now or dt.datetime.now(dt.timezone.utc)
    require(
        not Path(output).exists(), "Applied storage receipt exists; it is never renewed"
    )
    bound, durable, paths, installer, digest = inspect_jobs(
        repo, commit, tree, packet_dir
    )
    native = (native_factory or Native)(bound)
    _native_checks(repo, commit, bound, durable, digest, native, inventory)
    body = {
        "schema_version": 1,
        "kind": KIND,
        "source": {"commit": commit, "tree": tree},
        "jobs": {
            "release_id": bound["release_id"],
            "image_digest": digest,
            "packet_dir": str(Path(packet_dir).resolve()),
        },
        "files": {k: so.sha_bytes(path.read_bytes()) for k, path in paths.items()},
        "installer_receipts": installer,
        "storage_effects_sha256": so.fingerprint(storage_inventory(durable)),
        "captured_utc": now.isoformat(),
        "deferred_effect_ids": ["E-JOB-VIEWS"],
    }
    body["receipt_sha256"] = receipt_hash(body)
    with Path(output).open("x", encoding="utf-8") as file:
        file.write(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return body


def verify(
    manifest,
    bindings,
    packet_dir,
    *,
    repo=None,
    now=None,
    native_factory=None,
    inventory=None,
):
    path = receipt_path(manifest, packet_dir)
    if path is None:
        return None
    value = _read(path)
    require(
        bindings is not None and bindings.get("mode") == "services-only",
        "Composed readbacks require services bindings",
    )
    require(
        manifest["source"] == {"commit": bindings["target"], "tree": bindings["tree"]},
        "Foreign services storage manifest",
    )
    repo = Path(repo) if repo is not None else Path(__file__).resolve().parents[3]
    from core.setup.release.durable_prior import git

    require(
        git(repo, "rev-parse", "HEAD").strip() == bindings["target"]
        and git(repo, "rev-parse", "HEAD^{tree}").strip() == bindings["tree"]
        and not git(repo, "status", "--porcelain=v1").strip(),
        "Forward storage proof needs clean bound source",
    )
    now = now or dt.datetime.now(dt.timezone.utc)
    validate_receipt(
        value,
        bindings["target"],
        bindings["tree"],
        now=now,
        max_age_minutes=bindings["maxSmokeAgeMinutes"],
    )
    bound, durable, paths, installer, digest = inspect_jobs(
        repo, bindings["target"], bindings["tree"], value["jobs"]["packet_dir"]
    )
    require(
        value["files"] == {k: so.sha_bytes(p.read_bytes()) for k, p in paths.items()}
        and value["installer_receipts"] == installer,
        "Original completed jobs proof changed",
    )
    require(
        value["jobs"]["image_digest"] == digest
        and value["storage_effects_sha256"]
        == so.fingerprint(storage_inventory(durable)),
        "Storage source inventory differs",
    )
    effect = next(e for e in manifest["effects"] if e["effect_id"] == EFFECT_ID)
    require(
        effect["target"] == value["jobs"]["release_id"],
        "Applied receipt effect names another jobs attempt",
    )
    require(
        all(
            v["verdict"] == "pass"
            for e in durable["effects"]
            if e["apply_kind"] in ("schema", "warehouse_install")
            for v in e["compat"].values()
        ),
        "Jobs storage compatibility was not proved",
    )
    native = (native_factory or Native)(bound)
    _native_checks(repo, bindings["target"], bound, durable, digest, native, inventory)
    return so.sha_bytes(path.read_bytes())
