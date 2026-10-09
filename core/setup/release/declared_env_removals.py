"""The environment names a release deploy is declared to remove from a service, held as the SHA-256 of each name (lower
case hex of the UTF-8 text) so the names are not in the repository.

deploy_candidate.sh removes exactly the live names of f42-agent whose digest is listed here, and the services-only
readback expects those names gone from the candidate and its template. Nothing else is removed or tolerated. The release
paste reads the same live names through this module and shows the matches to the operator before the typed DEPLOY, so the
list the operator sees and the list the deploy acts on come from one function.

Release A: f42-agent carries two such variables on every live revision and deploy_flags.env does not set them. The module
is standard library only, so the deploy script can import it without the rest of the helper.

    py -3.13 -m core.setup.release.declared_env_removals --service f42-agent < describe.json

reads a gcloud run services describe --format=json document on stdin and prints the matched names, one per line."""
import hashlib
import json
import re
import sys

DECLARED_ENV_REMOVAL_DIGESTS: dict = {
    "f42-agent": ("33d9fa8645f6b19693e867b504665de15a7e14854e1a930165a7b90f9b87cfc0",
                  "366e51dbd94f0cdc07d4cb3ce2b7648136c105094ef6c5bb120edc26b0f7195d"),
}
PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def declared_removal(service, name):
    """True when the digest of this environment variable name is one the release declares it removes from the service."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest() in DECLARED_ENV_REMOVAL_DIGESTS.get(service, ())


def live_env_names(description):
    """The environment variable names on the template of a gcloud run services describe document."""
    containers = description.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    return [entry.get("name", "") for container in containers for entry in container.get("env", [])]


def declared_live_names(service, description):
    """The live names of the service that the release declares it removes, sorted."""
    return sorted({name for name in live_env_names(description) if declared_removal(service, name)})


def main(argv):
    if len(argv) != 2 or argv[0] != "--service" or argv[1] not in DECLARED_ENV_REMOVAL_DIGESTS:
        sys.stderr.write("declared_env_removals: usage: --service <a service with a declaration>, describe JSON on stdin\n")
        return 64
    try:
        description = json.load(sys.stdin)
    except ValueError:
        sys.stderr.write("declared_env_removals: stdin is not JSON\n")
        return 65
    if not isinstance(description, dict):
        sys.stderr.write("declared_env_removals: stdin is not a JSON object\n")
        return 65
    names = declared_live_names(argv[1], description)
    if not all(PLAIN_NAME.fullmatch(name) for name in names):
        sys.stderr.write("declared_env_removals: a declared variable name is not a plain identifier\n")
        return 65
    for name in names:
        sys.stdout.write(name + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
