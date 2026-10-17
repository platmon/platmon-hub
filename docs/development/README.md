# Development and validation

[Documentation index](../README.md) · [Architecture](../design/architecture.md) · [Acceptance criteria](mvp-acceptance.md)

Hub runs from a source checkout with Python 3.9+ and the standard library. No installation or frontend build is needed for a source run. Wheel installation is also supported; see [operations](../guides/operations.md#build-and-local-install). Node.js is needed only for the JavaScript syntax check below; PowerShell 7 is needed for the optional Windows soak harness.

## Development guides and evidence

| Guide | Contents |
| --- | --- |
| [Local checks](#local-checks) | Source layout, local checks, package validation, and CI. |
| [Release policy](release.md) / [Changelog](../../CHANGELOG.md) | Versioning, release procedure and artifacts, upgrade compatibility, evidence, and remaining rollout gates. |
| [Resource budget](resource-budget.md) | Measured limits, actual-device checks, and capacity recommendations. |
| [Benchmark index](benchmarks/README.md) | Reproduction commands and [synthetic plan](benchmarks/plan.md). |
| [Extended soak](benchmarks/long-soak.md) / [plan](benchmarks/long-soak-plan.md) | 90-minute runs, investigations, and remaining evidence. |
| [Earlier Windows soak](windows-soak.md) | Two-device 30-minute procedure and the scope of its recorded evidence. |
| [Fleet acceptance checklist](mvp-acceptance.md) | Baseline contract scenarios and hardware regression checks. |
| [Brand assets](branding.md) | Provenance, source paths, browser URLs, and update rules. |

## Repository layout

All paths in this table are relative to the repository root.

| Path | Purpose |
| --- | --- |
| `README.md` | Project overview, minimal startup commands, and documentation links. |
| `docs/guides/` | User instructions for setup, configuration, and operation. |
| `docs/design/` | Architecture, API contracts, storage schema, and ADRs. |
| `docs/development/` | Development instructions, release policy, and validation evidence. |
| `nodes.example.json` | Committed empty registry for a first run without device traffic. |
| `nodes.json` | Operator-created local registry, ignored by Git. Preserve existing contents. An external registry can be selected with an absolute `--nodes` path. |
| [hub/__main__.py](../../hub/__main__.py) | CLI entry point and listener startup. |
| [hub/registry.py](../../hub/registry.py) | JSON registry validation and stable node identity. |
| [hub/runtime.py](../../hub/runtime.py) | Bounded polling, scheduling, jitter, backoff, and shutdown. |
| [hub/reload.py](../../hub/reload.py) / [hub/nodes_cli.py](../../hub/nodes_cli.py) | Validated registry reload and local CLI administration. |
| [hub/history/](../../hub/history/) | SQLite projection, writer, bounded queries, events, and retention. |
| [deploy/](../../deploy/) | Windows launch/startup scripts, systemd, and proxy examples. |
| [pyproject.toml](../../pyproject.toml) | Package metadata, CLI entry point, and shipped documentation/assets. |
| [hub/protocol.py](../../hub/protocol.py) | Agent HTTP reads and per-endpoint schema validation. |
| [hub/state.py](../../hub/state.py) | Cached state, freshness, diagnostics, and overview projection. |
| [hub/http.py](../../hub/http.py) | Read-only Hub routes and static asset allowlist. |
| [hub/web/](../../hub/web/) | Dashboard HTML, CSS, JavaScript, and brand assets served by Hub. |
| [tests/](../../tests/) | Synthetic contract, state, HTTP, dashboard, timing, and port tests. |
| [scripts/soak-windows.ps1](../../scripts/soak-windows.ps1) | Windows measurement harness. |
| [.github/workflows/test.yml](../../.github/workflows/test.yml) | CI test matrix and Windows harness smoke. |
| `soak-results/` | Default local measurement output, ignored by Git. |

Run all commands from the root containing `hub/`, `tests/`, and `nodes.example.json`. Relative `--nodes` paths use the current working directory; they are not relative to the document or module. No registry is selected automatically.

## Local checks

**Windows PowerShell** (substitute `py -3` for `python` if needed):

```powershell
python -m unittest discover -s tests -v
python -m compileall -q hub tests
node --check hub/web/app.js
node --check hub/web/devices.js
node --check hub/web/data.js
```

**Linux:**

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q hub tests
node --check hub/web/app.js
node --check hub/web/devices.js
node --check hub/web/data.js
```

The fixtures use synthetic schema-1 status/stats, schema-2 observations, local HTTP test servers, and a fake monotonic clock. They do not use your private registry or replace [hardware acceptance checks](mvp-acceptance.md#3-hardware-smoke-run). For a manual startup check, follow the [Quick Start](../guides/quick-start.md).

## Package and browser validation

Run `python scripts/check-package.py` (Linux: `python3`) to build twice, compare wheel hashes, and validate an isolated installation, CLI, docs, and assets. The script uses temporary directories and installs setuptools/wheel there; it needs access to the package index. Use `--output-dir PATH` only when you want to retain candidate artifacts outside the checkout.

With Playwright available and an empty Hub on an explicit loopback test port, `node scripts/check-history-ui.cjs http://127.0.0.1:PORT` validates synthetic History interactions and layout. Replace `PORT` with the running test port. Playwright is development tooling, not a runtime dependency. Screenshots go to the OS temporary directory.
CI runs this check and `check-agent-ui.cjs` on Linux with Python 3.13 only, installing a pinned Playwright
outside the repository.

`node scripts/check-agent-ui.cjs http://127.0.0.1:PORT` checks the pinned 1.2.0
GPU/NPU/battery fixture, independent freshness, missing/zero/signed readings,
older agents, HTML escaping and light/dark mobile layouts. Unit tests additionally
cover schema-4-to-5 backup/rollback and mixed old/new retained summaries.

## CI

The [test workflow](../../.github/workflows/test.yml) runs on `ubuntu-latest` and `windows-latest` with Python 3.9 and 3.13, plus Python 3.10, 3.12 and 3.14 on `ubuntu-latest` for the Ubuntu LTS default interpreters (seven jobs; [ADR-0005](../design/adr/0005-os-and-python-support.md)). Each job runs unit tests, Python compilation, JavaScript syntax checks, synthetic fleet/soak smoke, and isolated package build/install validation. Linux jobs validate the systemd example and the Python 3.13 job checks the optional TLS proxy. Windows jobs validate launch/startup scripts without installing a service.

Windows jobs also start a loopback Hub with an empty registry and run a four-second measurement harness smoke. This checks script wiring and summary fields without reaching real agents. It does not replace the [30-minute Windows soak](windows-soak.md) or establish fleet capacity.

## Documentation maintenance

Keep the root README limited to the project purpose, minimum prerequisites, startup, and links. Put user instructions in `docs/guides/`, design references and ADRs in `docs/design/`, and development or validation material in `docs/development/`. Register each document in its directory README; [the documentation index](../README.md) links the three categories.

Use repository-root paths in commands and document-relative paths in Markdown links. After moving a document, check its incoming links, outgoing links, and section anchors. Preserve Windows/Linux command variants where they differ. When adding a documentation subdirectory, update the data-file mappings in [pyproject.toml](../../pyproject.toml) and run the package check so installed documentation keeps the same directory structure.

Keep operational defaults and limits in the [architecture reference](../design/architecture.md#default-polling-and-state-semantics), CLI and registry instructions in [setup](../guides/setup.md), and response fields in the [Hub API reference](../design/api.md). Service/backup instructions belong in [operations](../guides/operations.md), and complete CLI options in [configuration](../guides/configuration.md). Record measurements in the [resource budget](resource-budget.md) and linked benchmark reports with their scope; do not describe planned checks as completed validation.

## Replay validation

`tests/test_replay.py` checks complete stale payloads, missing IDs, malformed pages,
ordered migration rollback/backup, atomic cursor/loss commits, idempotent retries,
crash recovery, bounded admission, quota rollback, cursor retention, registry races,
HTTP limits/409/404/redirect handling, Live isolation and bounded JSONL exports.
[Raw replay synthetic plan](benchmarks/replay-plan.md) defines separate resource
budgets. Existing Live benchmark evidence does not establish raw deployment capacity.
