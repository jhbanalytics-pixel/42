from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.contracts.open_intelligence_fixtures import (
    DEFAULT_FIXTURE_DIRECTORY,
    FixtureValidationError,
    load_fixture_package,
)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) > 1:
        print("FAIL cli: argument_count_invalid")
        return 1
    directory = Path(arguments[0]) if arguments else DEFAULT_FIXTURE_DIRECTORY
    try:
        package = load_fixture_package(directory)
    except FixtureValidationError as error:
        print(f"FAIL {error}")
        return 1
    except Exception:
        print("FAIL verifier: unexpected_local_error")
        return 1
    print(f"fixture_count={package.fixture_count}")
    print(f"contract_version={package.contract_version}")
    print(f"manifest_sha256={package.manifest_sha256}")
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
