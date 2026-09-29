# SEC-03A — Vitest 4.1.11 and fast-uri 3.1.8

Status: implementation complete locally; **not** committed. SEC-03B and SEC-03C are **NOT STARTED**.

## Versions

| Package | Before | After | How |
| --- | --- | --- | --- |
| `vitest` (direct) | 3.2.4 | **4.1.11** | exact pin |
| `@vitest/mocker` | 3.2.4 via vitest | **4.1.11** via vitest | not a direct dependency |
| `fast-uri` | 3.1.5 | **3.1.8** | point update inside `ajv@8.20.0`'s existing `fast-uri` range `^3.0.1` |
| `vite` (Vitest) | 6.4.3 hoisted | **6.4.3** hoisted | unchanged |
| `vite` (Astro) | 8.3.1 nested | **8.3.1** nested | unchanged |
| `astro` | 7.3.5 | 7.3.5 | unchanged |
| `sharp` | 0.35.5 | 0.35.5 | unchanged |
| `devalue` | 5.9.4 | 5.9.4 | unchanged |
| `@astrojs/check` | 0.9.4 | 0.9.4 | unchanged |
| `js-yaml` | 4.3.2 | 4.3.2 | unchanged |
| `engines.node` | `>=22.12.0` | `^22.12.0 \|\| >=24.0.0` | drop Node 23 |

No overrides, no direct `fast-uri` or `@vitest/mocker` dependency, no `npm audit fix`.

## Dependency paths

- `vitest@4.1.11` → `@vitest/mocker@4.1.11` → `vite@6.4.3`
- `@astrojs/check@0.9.4` → `@astrojs/language-server@2.16.13` → `volar-service-yaml@0.0.71` → `yaml-language-server@1.23.0` → `ajv@8.20.0` → `fast-uri@3.1.8`. `ajv@8.20.0` declares `fast-uri` as `^3.0.1`; that range is not the version of ajv.
- `astro@7.3.5` → `vite@8.3.1` (nested) and `sharp@0.35.5`, `devalue@5.9.4`

Vite was **not** unified. Vitest 4.1.11 accepts Vite 6, 7, or 8. The lock already had a hoisted `vite@6.4.3` that satisfies that range, so npm kept it. Astro still nests `vite@8.3.1`.

## Advisories closed

Confirmed against OSV before install. `vitest@3.2.7` still carries GHSA-82fw; `fast-uri@3.1.6` still carries GHSA-qw65 and GHSA-58mr. Chosen versions report no OSV findings.

| ID | Closed by |
| --- | --- |
| [GHSA-82fw-gwwq-j7x9](https://github.com/advisories/GHSA-82fw-gwwq-j7x9) | vitest / `@vitest/mocker` 4.1.11 |
| [GHSA-5xrq-8626-4rwp](https://github.com/advisories/GHSA-5xrq-8626-4rwp) | vitest 4.1.11 |
| [GHSA-5jgf-p345-68v8](https://github.com/advisories/GHSA-5jgf-p345-68v8) | fast-uri 3.1.8 |
| [GHSA-f65p-4m7j-42xc](https://github.com/advisories/GHSA-f65p-4m7j-42xc) | fast-uri 3.1.8 |
| [GHSA-fph4-wmhf-6fwf](https://github.com/advisories/GHSA-fph4-wmhf-6fwf) | fast-uri 3.1.8 |
| [GHSA-jqff-g426-hqxp](https://github.com/advisories/GHSA-jqff-g426-hqxp) | fast-uri 3.1.8 |
| [GHSA-qw65-cvwx-89v3](https://github.com/advisories/GHSA-qw65-cvwx-89v3) | fast-uri 3.1.8 |

`js-yaml@4.3.2` was already at the fix for GHSA-2883-xcg3-v3hh and was not bumped.

## Node

Intersection used: Astro `>=22.12.0`, Vite 8 `^20.19.0 || >=22.12.0`, Vitest 4.1.11 `^20 || ^22 || >=24`. The project range is now `^22.12.0 || >=24.0.0`, which excludes Node 23. CI still requests `node-version: "22"` and asserts the resolved version matches that range.

| Place | Version | Role |
| --- | --- | --- |
| Local host | Node v25.6.1, npm 11.9.0 | satisfies `>=24`; not a Node 22 run |
| Disposable container | Node v22.23.3, npm 10.9.9 (`node:22-bookworm`) | Node 22 test run |
| CI | `node-version: "22"` plus the new assert | not executed in this session |
| Web builder image | `node:26.5.1-alpine` | unchanged; not a Node 22 run |

## Tests and limits

Script remains `vitest run`. Config is still `environment: "node"` with per-file jsdom comments. No UI, API, or browser server was added. `vitest.config.ts` was not changed.

| Run | Result |
| --- | --- |
| Host Node 25.6.1 | 36 files / 313 tests |
| Container Node 22.23.3 | 36 files / 313 tests |

Same counts as the SEC-02 baseline. Lint and `astro check` reported 0 errors. An earlier Node 22 attempt copied only `web/` and failed two suites that read `tests/fixtures` and `deploy/nginx` outside that directory. Those failures were the incomplete copy, not the suite. The full-repo container run above is the Node 22 result.

Production builds with media flow off and on, indexing off, kept `noindex,nofollow` and an empty sitemap. Disposable web image: Nginx 1.31.3 serves `dist`; no `node`, `npm`, `vitest`, or `node_modules`.

## Audit

On 2026-09-29, `npm audit --json --ignore-scripts --package-lock-only` exited 0. Metadata: 0 packages, 0 advisory IDs. That is the result of this lockfile check on that date. It does not check Python, FFmpeg, OS packages, PostgreSQL, Nginx, or production image digests, and it does not mean every vulnerability is absent.

No new npm findings. No exceptions accepted.

## Not started

- SEC-03B1 — Python lock and frozen install: `docs/operations/sec-03b-python-lock.md` (in progress in worktree; not committed)
- SEC-03B2 — npm and Python audit CI
- SEC-03C — local image scanning
