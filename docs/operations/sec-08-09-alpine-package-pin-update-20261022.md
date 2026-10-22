# Alpine v3.24 package pin update (2026-10-22)

Build-unblock for native storage acceptance run `39803810951`.

## Before (broken on current v3.24 main index)

| Package | Pin | Result |
|---------|-----|--------|
| libexpat | `2.8.5-r0` | **not found** (superseded) |
| pcre2 | `10.49-r0` | still listed |
| tiff | `4.7.2-r0` | still listed |

`apk add` with a missing pin fails the whole command (exit 4), so prepare/build stopped before container mounts.

## After

| Package | Pin | Advisory coverage |
|---------|-----|-------------------|
| libexpat | `2.9.0-r0` | CVE-2026-93990 fixed since ≥2.8.5-r0; 2.9.0 also includes CVE-2026-77214 / CVE-2026-102633 |
| pcre2 | `10.49-r0` | CVE-2026-103111 (unchanged) |
| tiff | `4.7.2-r0` | CVE-2026-4775 (unchanged) |

## Sources (official)

- APKINDEX: `https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/APKINDEX.tar.gz` → libexpat `2.9.0-r0`, pcre2 `10.49-r0`, tiff `4.7.2-r0`
- Package pages: https://pkgs.alpinelinux.org/package/v3.24/main/x86_64/libexpat (and pcre2, tiff)
- Security tracker: https://security.alpinelinux.org/vuln/CVE-2026-93990 , CVE-2026-103111, CVE-2026-4775

Same pins applied to `web/Dockerfile` and `deploy/nginx/Dockerfile` (base remains `nginx:1.31.6-alpine3.24`; no edge/distro switch).

New images are a new candidate; prior Image IDs / audit verdicts do not transfer.
