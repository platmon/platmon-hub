# Release candidate and upgrade policy

[Documentation index](../README.md) · [Development guide](README.md)

`0.2.0rc1` is a candidate application version, not an assertion of production
acceptance. Versions follow PEP 440; candidate increments precede a stable release.
Hub HTTP schema 1, SQLite schema 7 and Agent schemas remain separately versioned.
A code downgrade never implicitly downgrades a database.

## Candidate artifacts

```sh
python scripts/check-package.py --output-dir /temporary/candidate-artifacts
```

The check builds twice from independent source copies with the same isolated
setuptools/wheel environment and fixed `SOURCE_DATE_EPOCH` (default 1704067200),
requires identical SHA-256 hashes, installs the wheel into an isolated target and
checks CLI, examples and static assets. The output includes the validated wheel
and `package-report.json` with tool versions, Python/platform and hash. This proves
same-toolchain reproducibility, not identical output across all future build tools
or platforms. Build tools may access the package index; runtime has no third-party
dependencies. Retain the report and exact tool versions with candidate artifacts.
CI repeats this on Ubuntu/Windows with Python 3.9/3.13. The wheel includes deployment
examples and operational documentation under `share/platmon-hub`.

## Upgrade and downgrade

1. Preserve the previous wheel, protected registry and alert configuration.
2. Create a new verified online backup with `platmon-hub backup --db OLD_DB --output NEW_BACKUP`.
3. Stop the service and verify all Hub processes using that DB have exited.
4. Install the candidate into the intended venv; keep state outside code directories.
5. Start with the same registry and DB path. Verify version, node UUIDs, Live state,
   History queries, writer/reload diagnostics and alert restoration.
6. For rollback, stop Hub, preserve the failed database with its sidecars, restore a
   compatible verified backup to a clean path, restore matching configuration and
   install the compatible previous wheel. Obtain operator consent for observations
   lost after the backup. Never modify `user_version` to force compatibility.

The migration test starts with schema 1, preserves zero/null measurements through
schema 7, makes an online backup, preserves UUID through CLI editing and restores
a schema-1 backup independently. It does not simulate an installed OS service upgrade.
See [operations](../guides/operations.md) for service-specific paths and commands.

## Evidence and remaining acceptance gates

Completed evidence includes unit/integration and migration/failure tests, browser
syntax and actual Chromium light/dark/mobile History checks, authenticated local
TLS proxy tests, package installation/rebuild checks, synthetic 10/50/100-node and
failure scenarios, and read-only three-device collection on Linux Python 3.12 and
native Windows Python 3.13. Each real-device run collected for about 60 seconds,
then restarted a temporary Hub process for about 15 seconds: all three nodes were
Live/current, UUIDs and committed history survived, and no writer drops occurred.
These processes did not install services or alter Agents. Detailed synthetic
measurements and limitations are in [resource budget](resource-budget.md).

Before production acceptance, verify on the actual target hosts:

- Windows 11 scheduled startup without login, Linux systemd boot, service ACLs and
  installed-service upgrade/rollback. CI configuration parsing is not a reboot test.
- A longer deployment-specific pilot: the [90-minute follow-up](benchmarks/long-soak.md)
  passed absolute budgets and fixed instrumentation/cancellation defects. Some real
  working-set growth remains; an indefinite memory plateau is not established.
- Representative retained datasets, concurrent viewers and real storage capacity.
  The measured 150,000-row storage fixture is not a full 7-day 100-node database.
- Approved real-device communication failure/recovery. Fault injection so far used
  synthetic Agents; production Agents were neither restarted nor modified.

Release tag creation and operational deployment require separate approval. Candidate
PR merges and CI artifacts do not authorize installing services or publishing a release.
