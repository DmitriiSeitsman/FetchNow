# SEC-03B2 — npm and Python dependency audit gates

Status: merged (PR #157). SEC-03C image audit is a separate gate —
`docs/operations/sec-03c-image-audit.md`. Production unchanged by this document.

This gate runs metadata-only advisory checks against locked inputs. It is a dated
result from advisory services, not a guarantee that those databases are complete
or that unknown vulnerabilities are absent.

## Scope history (do not conflate)

1. **Original SEC-03B2 wrapper scope** did **not** change application lockfiles.
   Allowed paths were the audit wrapper, fixtures, CI job, exceptions file, and
   operations docs.
2. **Separately approved later:** a **dev-only** remediation of
   `pytest` 8.4.1 → 9.0.3 and `pytest-asyncio` 1.1.0 → 1.3.0 (plus
   `backend/uv.lock` / `backend/ci/pytest-requirements.txt` regeneration) to close
   PYSEC-2026-1845 / GHSA-6w46-j5rx-g56g / CVE-2025-71176. Runtime dependency set
   and Dockerfiles remained unchanged.

## Current paths

| Path | Role |
| --- | --- |
| `scripts/dependency_audit.py` | Bounded full three-section gate |
| `scripts/requirements/dependency-audit.txt` | Hashed tool-only pins for `pip-audit==2.10.1` |
| `docs/operations/dependency-exceptions.json` | Empty `exceptions: []` unless owner-approved |
| `tests/dependency_audit/` | Offline fixture unit tests |
| `.github/workflows/ci.yml` job `dependency-audit` | CI gate |

## Mandatory sections

The full gate always runs exactly:

1. `npm`
2. `python-runtime`
3. `python-dev`

Empty, partial, or duplicated section sets cannot exit 0 / `clean`. There are no
public `--skip-*` flags on the CLI.

## JSON contracts

### pip-audit 2.10.1

- Shape: object `{ "dependencies": [...], "fixes": [...] }` (not a bare array).
- Each resolved dependency **must** include `name`, `version`, and `vulns` (array).
  A missing `vulns` field is **not** treated as an empty list.
- Skipped dependencies (`skip_reason`) fail completeness.
- Inventory compare uses PEP 503-normalized names vs marker-filtered export
  (missing / unexpected / duplicate / wrong version).
- **JSON channel:** bounded **stdout** only (`-f json`). No `-o` file and no
  stdout/file fallback that can declare clean from an alternate channel.

Documented scanner exits: `0` clean inventory/findings-none; `1` vulnerabilities
present. Any other exit is a technical failure and cannot be cleared by
exceptions.

### npm audit report v2

- Requires `auditReportVersion == 2`, typed metadata counters (non-negative ints),
  and a `vulnerabilities` object.
- Metadata totals must not contradict the map (e.g. `total > 0` with empty map,
  or findings with `total == 0`).
- Empty `via`, or only unresolved string `via` references while metadata is
  nonzero, cannot be classified `clean`.
- String `via` entries (transitive pointers) are allowed; object `via` rows carry
  advisories. The vulnerabilities map is **not** an independent full lock
  inventory proof.
- Exception matching uses **installed versions** from `package-lock.json`
  `packages` entries referenced by vulnerability `nodes`, never the advisory
  range string. Unresolved nodes → no version → exception cannot clear.

Documented exits: `0` / `1` as above; other exits remain failures.

## Target filtering (Python)

Markers are evaluated once for the synthetic target environment. Surviving pins
are rewritten as `name==version` plus hashes **without** marker expressions so
pip-audit cannot re-filter by the scanner’s Python/platform.

| Section | Platform | Python markers | uv `--python` | extras |
| --- | --- | --- | --- | --- |
| python-runtime | linux x86_64 / CPython | 3.14 / **3.14.7** | **3.14.7** | `--no-dev` |
| python-dev | linux x86_64 / CPython | 3.12 / **3.12.0** | **3.12** | `--extra dev` |

CI installs `3.14.7` and `3.12` with `UV_PYTHON_DOWNLOADS=never` so export
interpreters match these targets. Dev markers use the documented `3.12.0` full
version; the export requests the `3.12` interpreter line from setup-python (patch
not pinned to a single build). Application `requires-python` is unchanged.

Local project `fetchnow` is excluded. Non-empty corrupt exports that silently
yield an empty external inventory fail (`export_silent_loss` /
`export_parse_error`), not `clean`.

## Exceptions

File: `docs/operations/dependency-exceptions.json` — currently empty.

Each exception requires: `ecosystem`, `scope` (`npm` | `python-runtime` |
`python-dev`), `advisory` (+ optional `aliases`), `package`, exact `versions`
(no ranges/wildcards), `reason`, `owner`, `review_deadline`.

Matching is intersection of advisory/alias, package, exact version, ecosystem,
and scope. Runtime exceptions do not apply to dev (and vice versa). Expired or
malformed rules do not apply.

Reports keep separately:

1. `raw_findings` — full normalized source findings
2. `accepted_exceptions` — scope + link to finding
3. `findings` — residual

Non-empty accepted exceptions must **not** be described as “0 vulnerabilities”.

## Budgets / privacy

| Step | Budget |
| --- | --- |
| npm audit | 60 s |
| each pip-audit | 90 s |
| uv lock --check | 30 s |
| uv export | 60 s |
| CI job | 10 minutes |
| per-stream / JSON stdout | 2 MiB |

Timeouts, cancellation, and output limits cannot become `clean`. Published
diagnostics use allowlisted `error_code` / `error_context` only; credentials,
tokens, private registry userinfo, and raw stderr/environment are redacted or
omitted.

On npm section failure the report may also include **safe digests only**:
scanner returncode, stdout/stderr byte sizes and SHA-256, a fixed
`failure_stage` (`decode` | `schema` | `scanner_exit` | `bounded_execution`),
and a fixed `diagnostic_reason`. When JSON decoded successfully, only root
type, presence of expected top-level field *names*, numeric
`auditReportVersion`, boolean `has_npm_error_object`, and an `E…` shaped
`npm_error_code` (when present) are recorded. Raw npm payloads, free-form
messages, URLs, and environment are never copied into the report.

## First scanner_error chronology (historical)

During remediation acceptance (not proven fixed by later wrapper edits alone):

| UTC | Event |
| --- | --- |
| 2026-09-29 21:36:33 | Full gate: `python-runtime` `scanner_error` / no JSON |
| 2026-09-29 21:36:43 | Separate successful runtime-only diagnostic run |
| 2026-09-29 21:37:09 | Separate successful full audit |

Exact root cause of the first failure was **not** proven. Do not claim these
acceptance edits fixed that incident without evidence. A historical full-audit
`clean` from the previous wrapper is **not** proof that the remediated wrapper
passes an end-to-end advisory gate; a controlled real audit is a later step.

## Tooling install

Isolated audit venv only (never `backend/.venv` / runtime image). Bootstrap
`pip` from the newly created venv installs the hashed tool requirements —
there is **no** floating pip upgrade step in CI. Bootstrap pip
itself is not hash-pinned; reproducibility is claimed for the hashed audit
tool set (`scripts/requirements/dependency-audit.txt`), not for the venv
bootstrap interpreter/pip.

```bash
python3.12 -m venv /tmp/fetchnow-dependency-audit
/tmp/fetchnow-dependency-audit/bin/python -m pip install --require-hashes --no-deps \
  -r scripts/requirements/dependency-audit.txt
```

Pinned scanners: **pip-audit 2.10.1**, service **PyPI** (`-s pypi`); uv **0.12.19**.
The wrapper probes observed `--version` output **before** export/scanner and
fails closed on mismatch / unreadable probes. Recording expected versions in
the JSON report does not satisfy that check.

## Offline fixture tests

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3.12 -m pytest -q tests/dependency_audit
```

## Residual risks

- Advisory DB lag; dated snapshot only
- Marker filtering follows `uv export` pin lines (not full lock-graph edge re-walk)
- npm metadata ≠ independent lock inventory enumeration
- npm `via` reachability proves evidence paths; it does not rebuild the install graph
- Bootstrap pip inside the tool venv is not hash-pinned (hashed packages are)
- Historical transient empty-JSON runtime failure cause unproven
- OS / FFmpeg / Postgres / Nginx / images → SEC-03C