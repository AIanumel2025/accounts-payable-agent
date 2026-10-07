#!/usr/bin/env python3
"""Proves the *installed* `ap_agent` package can load every migration (M11E.2 packaging regression check).

Run it inside the built migration image (and against a freshly installed wheel in the unit test): it imports `ap_agent` from
wherever it is installed -- NOT from the repository source tree -- calls `load_all_migrations()` and verifies

  * exactly five migrations load,
  * their IDs equal `MIGRATION_MANIFEST` in order,
  * every checksum equals the manifest's immutable `expected_checksum`,
  * (with `--require-installed`) the package really came from a `site-packages`/`dist-packages` directory.

Exit 0 on success; non-zero with a one-line reason otherwise. It never touches a database.
"""

from __future__ import annotations

import argparse
import sys

EXPECTED_COUNT = 5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--require-installed", action="store_true", help="fail unless ap_agent was imported from site-packages")
    parser.add_argument("--expect-prefix", default=None, help="fail unless ap_agent was imported from below this directory")
    arguments = parser.parse_args()

    import ap_agent
    from ap_agent.db.migration_manifest import MIGRATION_MANIFEST
    from ap_agent.db.migration_runner import MIGRATIONS_DIRECTORY, load_all_migrations

    location = str(ap_agent.__file__)

    if arguments.require_installed and not ("site-packages" in location or "dist-packages" in location):
        print(f"FAIL: ap_agent was imported from {location}, not from an installed package.", file=sys.stderr)
        return 2

    if arguments.expect_prefix and not location.startswith(arguments.expect_prefix):
        print(f"FAIL: ap_agent was imported from {location}, expected below {arguments.expect_prefix}.", file=sys.stderr)
        return 2

    try:
        loaded = load_all_migrations()
    except Exception as error:  # noqa: BLE001 - report the class and message, never a traceback of secrets (there are none)
        print(f"FAIL: load_all_migrations() raised {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    if len(loaded) != EXPECTED_COUNT or len(MIGRATION_MANIFEST) != EXPECTED_COUNT:
        print(f"FAIL: expected {EXPECTED_COUNT} migrations, loaded {len(loaded)} (manifest has {len(MIGRATION_MANIFEST)}).", file=sys.stderr)
        return 1

    for position, (migration, entry) in enumerate(zip(loaded, MIGRATION_MANIFEST), start=1):
        if migration.migration_id != entry.migration_id:
            print(f"FAIL: position {position}: loaded {migration.migration_id}, manifest says {entry.migration_id}.", file=sys.stderr)
            return 1

        if migration.checksum != entry.expected_checksum:
            print(f"FAIL: {entry.migration_id}: checksum {migration.checksum} != manifest {entry.expected_checksum}.", file=sys.stderr)
            return 1

    print(f"OK: {len(loaded)} migrations loaded from {MIGRATIONS_DIRECTORY}")

    for migration in loaded:
        print(f"  {migration.migration_id}  {migration.checksum}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
