"""Route enumeration for f42-api and f42-agent against core/api/contract.md.

Pure functions, so the test file can run them on the real apps and on small apps built to be wrong.
"""
import re

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
CONTRACT_ROUTE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE)\s+(/[^`\s]*)`")
PARAMETER = re.compile(r"\{[^}]*\}")


def normal_path(path):
    """A path with every parameter, typed or not, written as {} so contract names and code names compare."""
    return PARAMETER.sub("{}", path.split("?", 1)[0])


def contract_routes(text):
    """{(METHOD, path)} for every `METHOD /path` token the contract writes in backticks."""
    return {(m.group(1), normal_path(m.group(2))) for m in CONTRACT_ROUTE.finditer(text)}


def catch_all(route):
    return ":path}" in getattr(route, "path", "")


def app_routes(app):
    """(routes, catch_alls): {(METHOD, path)} for the routes a client can call by name, and the catch-all routes
    (a {name:path} pattern) as {(METHOD, path)}. HEAD and OPTIONS are not counted. A mount has no method and is
    not counted."""
    named, catch_alls = set(), set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        target = catch_alls if catch_all(route) else named
        for method in methods:
            if method in HTTP_METHODS:
                target.add((method, normal_path(route.path)))
    return named, catch_alls


def concrete(path):
    """A path with a value in each parameter, to call it."""
    return PARAMETER.sub("x", path.replace("{}", "{x}"))


def undocumented(routes, documented):
    return sorted(set(routes) - set(documented))


def missing(documented, *served):
    served_all = set().union(*served)
    return sorted(set(documented) - served_all)


def label(service, method, path):
    return f"{service} {method} {path}"
