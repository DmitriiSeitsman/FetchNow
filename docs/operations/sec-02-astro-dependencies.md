# SEC-02 — Astro 7.3.5 dependency migration

Status: implementation complete locally; **not** committed. Owner visual QA: PASS.

## Goal

Close Astro/Sharp production-build advisories by upgrading Astro `5.12.8` → `7.3.5`,
keep static Nginx delivery, and bind `astro dev` / `astro preview` to loopback by default
with an explicit LAN opt-in.

## Versions

| Component | Before | After |
| --- | --- | --- |
| `astro` (direct) | 5.12.8 | **7.3.5** |
| `sharp` (via Astro) | vulnerable transitive (≤0.34.x advisory range) | **0.35.5** (single copy under `astro`) |
| `vite` (Astro) | Vite 6 via Astro 5 | **8.3.1** nested under `astro` |
| `vite` (Vitest) | 6.x | **6.4.3** (Vitest tree; unchanged direct Vitest) |
| `vitest` | 3.2.4 | **3.2.4** (unchanged; SEC-03) |
| `@astrojs/check` | 0.9.4 | 0.9.4 (no bump required) |
| `engines.node` | `>=22` | `>=22.12.0` (Astro 7.3.5) |
| `@types/node` | transitive only (dropped under Vite 8) | **22.18.6** direct `devDependency` |

Resolved copies confirmed with `npm ls` / `npm explain` after clean `npm ci`:

- one `astro@7.3.5`
- one `sharp@0.35.5` (optional dep of Astro)
- no invalid / unmet peer dependencies

## Compatibility changes

1. **`web/package.json` / lockfile** — pin Astro 7.3.5; tighten engines; add `@types/node`
   because Vite 8 no longer installs Node types transitively and `astro check` includes
   test files that import `node:fs` / `node:path` / `node:url`.
2. **`web/astro.config.mjs`** — keep `output: "static"` and explicit `compressHTML: true`
   (Astro 7 default is `'jsx'`, which can drop spaces between adjacent inline elements).
   Replace `host: true` with loopback default (`127.0.0.1`) and LAN opt-in via
   `FETCHNOW_ASTRO_HOST=lan|true|0.0.0.0|*`.
3. **`.github/workflows/ci.yml`** — keep `node-version: "22"`; add a runtime assert that the
   resolved Node is `>=22.12.0` (CI strings alone do not guarantee a minimum patch).
4. **Dockerfile / Nginx ports** — unchanged. Builder `node:26.5.1-alpine` already satisfies
   engines; runtime remains `nginx:1.31.3-alpine` on container port `8080`.

No Vitest bump, no `--force` / `--legacy-peer-deps` / overrides, no product/UI redesign,
no SSR introduction.

## Dev / preview networking

| Mode | Default listen | LAN opt-in |
| --- | --- | --- |
| `npm run dev` | `127.0.0.1:4321` | `FETCHNOW_ASTRO_HOST=lan npm run dev` |
| `npm run preview` | `127.0.0.1:4321` | same env |

Evidence (local): Astro log `Local http://127.0.0.1:4321/` and `lsof` showed
`TCP 127.0.0.1:4321 (LISTEN)` for both commands. Production gateway/web ports are unrelated.

## Acceptance (automated)

| Check | Result |
| --- | --- |
| Clean `npm ci` from final lockfile | PASS (501 packages) |
| `npm test` | PASS — **36** files / **313** tests |
| `npm run lint` | PASS |
| `npm run typecheck` (`astro check`) | PASS — 0 errors |
| Production `astro build` (MF off / on, indexing off) | PASS — 12 pages |
| Routes in `dist/` | PASS — `/`, `/premium/`, `/offer/`, `/terms/`, `/privacy/`, `/copyright/`, payment success/fail, providers |
| SEO contracts | PASS — meta `noindex,nofollow`, empty sitemap when indexing off; CSP/Robokassa covered by existing unit tests |
| Disposable Docker web image | PASS — Nginx serves `dist`; no `node`/`npm`/`astro`/`node_modules`/secrets in runtime |
| Compose `web` contract | PASS — static image build args; no host ports in base compose |
| `git diff --check` | PASS |

Manual visual QA by owner: **PASS**.

## npm audit (lockfile metadata only)

Scope: npm dependency advisories. Does **not** cover OS, Python, FFmpeg, or Nginx.

### Before (Astro 5.12.8 lock)

- Packages affected: **8**
- Distinct GHSA IDs: **31** (incl. Astro critical set + Sharp high + Vitest/dev tooling)

### After Astro 7.3.5, before the devalue lock bump

- Packages affected: **4** (`devalue`, `vitest`, `@vitest/mocker`, `fast-uri`)

### After the devalue lock bump (`npm audit --json --ignore-scripts --package-lock-only`)

- Packages affected: **3** (`vitest`, `@vitest/mocker`, `fast-uri`)
- Distinct GHSA IDs: **7** (Vitest 2, fast-uri 5). `devalue` is no longer reported.
- Metadata totals: moderate 1, high 1, critical 1 (npm counts packages, not advisory IDs).

## devalue (GHSA-9rgm-9g3h-6x36)

Official advisory: patched in **5.9.2** (affected `< 5.9.1`).
Astro 7.3.5 depends on `devalue@^5.8.1`, which already allows 5.9.2+.

| | |
| --- | --- |
| Before | `5.9.0` via `astro@7.3.5` |
| After | `5.9.4` (latest version inside `^5.8.1`; includes the 5.9.2 bounds check) |
| Method | `npm update devalue` — one lockfile package, no direct dependency, no override |

Installed `src/parse.js` rejects out-of-range indexes (`index >= values.length`) and invalid array indexes (`is_valid_array_index`).

Where Astro calls it:

- content data-store / mutable data-store `devalue.parse` of local content-layer records at build time. This site has no content collections.
- Astro Actions client `devalue.parse` of action responses. This site does not use Astro Actions. Production client bundles (`dist/_astro/*.js`) contain no devalue code.
- session `unflatten` is an SSR session helper and is not part of the static Nginx output.

The vulnerable `parse` is therefore not invoked on untrusted visitor input in the shipped static site. The advisory is closed in the lockfile anyway, not waived.

### Resolved by SEC-02

All prior `astro` and `sharp` GHSA entries from the pre-upgrade audit, including
AVIF/path advisories that required Astro ≥7.2.8 (for example
[GHSA-26w7-cxv4-gfx2](https://github.com/advisories/GHSA-26w7-cxv4-gfx2)).
Also cleared from this lock relative to baseline: `js-yaml`, `nanoid`.

### Remaining (defer to SEC-03 / owner)

| Package | Path | Advisory | Execution layer | Next |
| --- | --- | --- | --- | --- |
| `vitest@3.2.4` / `@vitest/mocker@3.2.4` | direct `vitest` → `@vitest/mocker` | [GHSA-82fw-gwwq-j7x9](https://github.com/advisories/GHSA-82fw-gwwq-j7x9), [GHSA-5xrq-8626-4rwp](https://github.com/advisories/GHSA-5xrq-8626-4rwp) | Dev/test only; UI server not used (`vitest run`). Not in the Nginx image. | **SEC-03** |
| `fast-uri@3.1.5` | `@astrojs/check` → `@astrojs/language-server` → `volar-service-yaml` → `yaml-language-server` → `ajv` → `fast-uri` | [GHSA-5jgf-p345-68v8](https://github.com/advisories/GHSA-5jgf-p345-68v8), [GHSA-f65p-4m7j-42xc](https://github.com/advisories/GHSA-f65p-4m7j-42xc), [GHSA-fph4-wmhf-6fwf](https://github.com/advisories/GHSA-fph4-wmhf-6fwf), [GHSA-jqff-g426-hqxp](https://github.com/advisories/GHSA-jqff-g426-hqxp), [GHSA-qw65-cvwx-89v3](https://github.com/advisories/GHSA-qw65-cvwx-89v3) | Typecheck/language-server tooling only. Not in the Nginx image. | **SEC-03** |

Vitest 3.2.4 did **not** block install or tests against Astro 7 / Vite 8 (separate Vite copies).

## Preserved contracts

- Static output → Nginx runtime; no SSR/Node server in production
- MediaFlow + browser grants; Free/Premium UI; TEST checkout amount server-side
- Offer/legal copy surfaces unchanged
- noindex + empty sitemap while indexing disabled; LIVE/SEO not enabled
- CSP `form-action 'self' https://auth.robokassa.ru` (gateway Nginx + unit test)

## Stop line

No commit / push / PR / production change in this step.
