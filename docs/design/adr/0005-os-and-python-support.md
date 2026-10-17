# ADR-0005: Hub operating system and Python support policy

[Documentation index](../../README.md) · [Design reference](../README.md)

- **Status:** Proposed (draft for review)
- **Date:** 2026-10-17
- **Scope:** Interpreters and operating systems on which Hub runs, and the CI evidence required for them
- **Hub baseline:** `main` at `fb5f0eed5b6268a58d05077281ef7de2779a5d36`
- **Related:** [platmon ADR 0001: Ubuntu LTS and Python support policy](https://github.com/platmon/platmon/blob/main/docs/development/adr/0001-ubuntu-lts-python-support.md), [hub ADR-0004](0004-downstream-data-handoff.md)

## Context

Hub declares `requires-python = ">=3.9"` and is documented for Linux and native Windows, but
it has no stated support policy. CI runs Python 3.9 and 3.13 on `ubuntu-latest` and
`windows-latest`.

platmon ADR 0001 makes Ubuntu 22.04, 24.04 and 26.04 native support targets for the Agent,
with default interpreters Python 3.10, 3.12 and 3.14. Hub's systemd example runs from
`/opt/platmon-hub/venv`, which an operator normally creates from the host's `python3`. A Hub
installed next to an Agent on those releases therefore runs on interpreters that Hub CI never
exercises. The local Hub suite passed on Python 3.14.4 on 2026-10-17, which is evidence for one
commit, not a policy.

## Decision

Align Hub's Linux interpreter coverage with the Agent's Ubuntu LTS targets, keep the existing
Windows coverage, and keep the Python 3.9 minimum. Hub remains a standard-library-only runtime.

| Environment | Policy |
| --- | --- |
| Python 3.9 and 3.13 on Linux and Windows | Existing support; tested in CI on both |
| Ubuntu 22.04 / Python 3.10 | Native support target; interpreter tested in CI on Linux |
| Ubuntu 24.04 / Python 3.12 | Native support target and development baseline; interpreter tested in CI on Linux |
| Ubuntu 26.04 / Python 3.14 | Native support target; interpreter tested in CI on Linux |
| Windows 11 x64 with a python.org interpreter 3.9 or later | Support target; CI covers 3.9 and 3.13 only |
| Python 3.8 / Ubuntu 20.04 | Outside support; no observation target, because the package cannot install there |
| Interim Ubuntu releases, future Python versions | Not qualified automatically |

A passing interpreter job is evidence about that interpreter, not qualification of the OS:
`ubuntu-latest` and `windows-latest` do not reproduce each release's systemd, filesystem or
service-manager behaviour. Native qualification of an OS combination follows the checks in
[release](../../development/release.md) on a booted VM or device, recorded with OS, Python and
architecture, as platmon ADR 0001 requires for the Agent.

Linux-only additions are deliberate. Windows users install a python.org interpreter of their
choice, and the existing 3.9 and 3.13 jobs bound that range; extending Windows jobs is a
separate decision based on observed failures or user reports.

## Consequences for other decisions

- New Hub features list "every supported interpreter" in their acceptance criteria rather than
  naming versions; ADR-0004 is updated accordingly.
- CI gains three Linux jobs. Steps that are already pinned to Python 3.13 (TLS proxy and browser
  checks) stay pinned; the new jobs run the unit suite, compilation, smoke harnesses, package
  build and systemd validation.
- Raising the minimum Python or removing a support target requires a changelog entry and a
  separate decision. The release-version rule for such removals is left open until Hub 1.0; see
  below.

## Alternatives

| Alternative | Decision |
| --- | --- |
| Keep 3.9 and 3.13 only | Reject: leaves the interpreters Hub actually runs on next to supported Agents untested |
| Mirror platmon's full matrix, including Python 3.8 observation | Reject: Hub cannot be installed on 3.8, so the job would provide no deployment evidence |
| Add every interpreter on Windows as well | Defer: doubles Windows CI time without a known Windows-specific interpreter risk |
| Raise the minimum to 3.10 | Reject for now: removes existing support without a demonstrated need |

## Open questions

1. Before Hub 1.0, does removing a support target require a minor release (pre-1.0 convention)
   or should Hub adopt platmon's major-release rule and six-month notice now?
2. Should Windows 10 remain implicitly supported, or be stated as outside support?

## Implementation

1. Add Python 3.10, 3.12 and 3.14 Linux jobs to the test workflow.
2. Update the development README's description of the CI matrix.
3. Replace fixed interpreter lists in ADR-0004's acceptance criteria with the supported set.
