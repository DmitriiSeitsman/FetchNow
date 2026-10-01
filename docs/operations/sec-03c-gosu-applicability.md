# SEC-03C — gosu Go-stdlib applicability (Stage 1)

Status: evidence review only. **Does not** change the image-audit gate, add
VEX/exceptions/ignores, modify images, or accept residual risk.

Analysis UTC: `2026-09-30T16:50:14Z`  
Parent image-audit summary SHA-256:
`22f612f8cfbe5999f50a4ea3afd24f28c43f0b6751bb70c3ef85792713f0e49f`
(`/tmp/fetchnow-sec03c-remediation/summary.json`).

## Artifact identity

PostgreSQL candidate: official `postgres` **linux/amd64** platform manifest
(immutable), not a floating tag.

| Field | Value |
| --- | --- |
| Registry index digest | `sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea` |
| linux/amd64 manifest digest | `sha256:1a66d744c1b459e13b05a8fca341da84cb63383e99ce262210efee5a319d4551` |
| Registry config digest | `sha256:81bd698b4594e751a3269e4dcd3e03a4a0ec0daf7b72e7aa1abd43cce9887542` |
| Docker `inspect .Id` (this host, after `docker pull --platform linux/amd64 postgres@sha256:1a66d744…`) | `sha256:1a66d744c1b459e13b05a8fca341da84cb63383e99ce262210efee5a319d4551` |
| Inspect platform | `linux/amd64` |

On this Docker image store, `inspect .Id` for the amd64 **manifest** pull equals
the platform **manifest** digest, not the registry **config** digest. Those
fields are recorded separately and are not treated as interchangeable Image ID
semantics for the gate. Extraction used
`postgres@sha256:1a66d744c1b459e13b05a8fca341da84cb63383e99ce262210efee5a319d4551`
only (`docker create` + `docker cp`; no Postgres process, volumes, credentials,
or production network).

### gosu binary under test

| Field | Value |
| --- | --- |
| Path in image | `/usr/local/bin/gosu` (Trivy Target `usr/local/bin/gosu`) |
| Also | `/usr/local/bin/su-exec` → symlink to `gosu` |
| File | ELF 64-bit LSB executable, x86-64, statically linked, not stripped |
| BuildID | `sha1:8cb1150240d17e1c6e4b0bad039d14e32d26e5c4` |
| SHA-256 | `52c8749d0142edd234e9d6bd5237dff2d81e71f43537e2f4f66f75dd4b243dd0` |
| Official release asset | `https://github.com/tianon/gosu/releases/download/1.19/gosu-amd64` — **byte-identical** (same SHA-256) |
| Go buildinfo | `go1.24.6`; module `github.com/tianon/gosu@v1.19.0`; `GOOS=linux` `GOARCH=amd64`; `CGO_ENABLED=0`; deps `github.com/moby/sys/user@v0.1.0`, `golang.org/x/sys@v0.1.0`; `vcs.revision=1.19` |

Embedded Go **1.24.6** is confirmed from `go version -m` on the extracted binary
(not only from Trivy text).

## Trivy findings in scope (22)

Source: remediation `summary.json` + `trivy-work/postgres.trivy.json`.
Package: `stdlib@v1.24.6`, `pkg_type=gobinary`, Target `usr/local/bin/gosu`,
DataSource Go Vulnerability Database. `PkgPath` was null in Trivy (path is the
Result Target).

| CVE | Sev | Go advisory | Vulnerable package/symbols (OSV) | Fixed (Trivy) |
| --- | --- | --- | --- | --- |
| CVE-2025-61726 | HIGH | GO-2026-4341 | `net/url` ParseQuery / URL.Query | 1.24.12, 1.25.6 |
| CVE-2025-61729 | HIGH | GO-2025-4155 | `crypto/x509` Certificate.Verify* | 1.24.11, 1.25.5 |
| CVE-2025-68121 | CRITICAL | GO-2026-4337 | `crypto/tls` Handshake/Dial/Read/Write | 1.24.13, 1.25.7, … |
| CVE-2026-25679 | HIGH | GO-2026-4601 | `net/url` Parse / parseHost | 1.25.8, 1.26.1 |
| CVE-2026-27145 | HIGH | GO-2026-5037 | `crypto/x509` Verify / matchHostnames | 1.25.11, 1.26.4 |
| CVE-2026-32280 | HIGH | GO-2026-4947 | `crypto/x509` Verify / buildChains | 1.25.9, 1.26.2 |
| CVE-2026-32281 | HIGH | GO-2026-4946 | `crypto/x509` Verify / policiesValid | 1.25.9, 1.26.2 |
| CVE-2026-32283 | HIGH | GO-2026-4870 | `crypto/tls` KeyUpdate / Handshake | 1.25.9, 1.26.2 |
| CVE-2026-33811 | HIGH | GO-2026-4981 | `net` LookupCNAME | 1.25.10, 1.26.3 |
| CVE-2026-33814 | HIGH | GO-2026-4918 | `net/http` + `golang.org/x/net/http2` | 1.25.10, 1.26.3 |
| CVE-2026-33818 | HIGH | GO-2026-5972 | `encoding/asn1` Unmarshal | 1.25.13, 1.26.6, … |
| CVE-2026-39820 | HIGH | GO-2026-4986 | `net/mail` ParseAddress* | 1.25.10, 1.26.3 |
| CVE-2026-39821 | HIGH | GO-2026-5026 | `net/http` + idna | 1.25.13, 1.26.6, … |
| CVE-2026-39822 | HIGH | GO-2026-4970 | `os` Root / OpenInRoot | 1.25.12, 1.26.5, … |
| CVE-2026-39836 | HIGH | GO-2026-4971 | `net` Dial/Lookup* (incl. Windows NUL) | 1.25.10, 1.26.3 |
| CVE-2026-42499 | HIGH | GO-2026-4977 | `net/mail` consumePhrase | 1.25.10, 1.26.3 |
| CVE-2026-42504 | HIGH | GO-2026-5038 | `mime` WordDecoder.DecodeHeader | 1.25.11, 1.26.4 |
| CVE-2026-56853 | HIGH | GO-2026-6089 | `net/http` Serve / ListenAndServe | 1.25.13, 1.26.6, … |
| CVE-2026-56858 | HIGH | GO-2026-6091 | `html/template` Execute* | 1.25.13, 1.26.6, … |
| CVE-2026-56859 | HIGH | GO-2026-6088 | `encoding/xml` Decoder.* | 1.25.13, 1.26.6, … |
| CVE-2026-56860 | HIGH | GO-2026-6218 | `net/url` ResolveReference / resolvePath | 1.25.13, 1.26.6, … |
| CVE-2026-56862 | HIGH | GO-2026-6090 | `crypto/tls` post-handshake KeyUpdate | 1.25.13, 1.26.6, … |

All 22 GO-IDs were resolved in the Go/OSV vulnerability database
(`https://api.osv.dev`, `https://vuln.go.dev`).

## What gosu actually does

Upstream sources for tag `1.19` (`github.com/tianon/gosu`):

- Direct imports: `os`, `os/exec`, `runtime`, `syscall`, `github.com/moby/sys/user`,
  `golang.org/x/sys/unix`.
- Behavior: parse `user-spec`, refuse setuid/setgid install, `SetupUser` (passwd/
  group + `Setuid`/`Setgid`/`Setgroups`), `exec.LookPath`, `syscall.Exec` into
  the target command. No HTTP server, TLS client, URL parser, mail/MIME, XML,
  ASN.1, or `html/template` usage in gosu’s own code.

Postgres entrypoint uses `exec gosu postgres …` for privilege drop only.

## Methodology

| Item | Value |
| --- | --- |
| Tool | `govulncheck@v1.1.4` (pin from upstream `govulncheck-with-excludes.sh`) |
| Install | `go install golang.org/x/vuln/cmd/govulncheck@v1.1.4` inside official `golang:1.24.6` container |
| Go toolchain for analysis | `go1.24.6 linux/amd64` |
| DB | `https://vuln.go.dev` — DB updated **2026-09-28 16:43:40 +0000 UTC** |
| Binary mode | `govulncheck -mode=binary -show verbose` on extracted `/wd/gosu` |
| Source mode | `govulncheck -show verbose ./...` on `gosu-1.19` source tree |
| Upstream SECURITY.md | https://raw.githubusercontent.com/tianon/gosu/1.19/SECURITY.md |
| Upstream wrapper | https://raw.githubusercontent.com/tianon/gosu/1.19/govulncheck-with-excludes.sh |

### Wrapper exclusions (read; **not** applied)

The wrapper excludes only:

- `GO-2023-1840` / `CVE-2023-29403` — rationale in-script: setuid mitigated in gosu code.

**None** of the 22 Trivy findings are in that exclusion list. This review used
**raw** `govulncheck` (no wrapper filtering). Upstream SECURITY.md’s general
“many stdlib CVEs do not apply” statement is **not** treated as per-CVE proof.

### Tool results (summary)

Both binary and source modes:

- **Symbol Results:** no vulnerabilities (0 called).
- **Package Results:** 3 imported-package hits (includes `GO-2026-4970` /
  CVE-2026-39822 among them).
- **Module Results:** 42 required-module / stdlib hits (includes the other 21
  GO-IDs from the Trivy set).
- Exit text: “Your code is affected by **0** vulnerabilities… packages you
  import / modules you require … doesn’t appear to call these vulnerabilities.”

Binary-mode is a static approximation of call reachability from the binary; it
is **not** a dynamic runtime exploitability proof. Source-mode matches the same
release (`v1.19.0`, byte-identical official `gosu-amd64`). Agreement of both
modes strengthens the classification but does not prove runtime behavior under
every possible loader/environment.

## Classification counts

| Verdict | Count |
| --- | --- |
| NOT_AFFECTED | **22** |
| AFFECTED | **0** |
| UNKNOWN | **0** |

Definition used here:

- **NOT_AFFECTED** — GO advisory present in vuln DB; `govulncheck@v1.1.4`
  Symbol Results do **not** list the GO-ID (not called); finding appears only
  under Package and/or Module Results (or equivalent “not called” outcome).
- **AFFECTED** — would require Symbol Results (called) for that GO-ID.
- **UNKNOWN** — missing DB entry, tool failure, or inability to map the finding.

Absence of an HTTP server alone is **not** the proof; classification is based
on OSV symbol lists + govulncheck call analysis of this binary/source.

## Per-advisory verdicts

Shared proof for all rows below unless noted:

1. Trivy reports `stdlib@v1.24.6` on gobinary `usr/local/bin/gosu`.
2. Extracted binary is official gosu 1.19 amd64 (hash match) with `go1.24.6`.
3. OSV lists vulnerable symbols in packages gosu does not call for its
   privilege-drop path.
4. `govulncheck` Symbol Results omit the GO-ID; Module and/or Package Results
   may still list it (stdlib present in the binary).

### Group A — `net/http` / HTTP2 / idna / TLS / x509 (network & PKI APIs)

**IDs:** CVE-2025-61729, CVE-2025-68121, CVE-2026-27145, CVE-2026-32280,
CVE-2026-32281, CVE-2026-32283, CVE-2026-33814, CVE-2026-39821, CVE-2026-56853,
CVE-2026-56862  

**Verdict:** NOT_AFFECTED for each ID above.  

**Mechanism:** OSV symbols are `crypto/tls`, `crypto/x509`, `net/http`,
`net/http/internal/http2`, and/or `golang.org/x/net/http2|idna` client/server
APIs. gosu does not call them; govulncheck lists these only under Module
Results, not Symbol Results. Exploitation conditions (TLS handshake, HTTP
serve/client, cert verify) are outside gosu’s exec/user-switch surface.

### Group B — `net/url`, `net` lookup/dial, mail, MIME, XML, ASN.1, html/template

**IDs:** CVE-2025-61726, CVE-2026-25679, CVE-2026-33811, CVE-2026-33818,
CVE-2026-39820, CVE-2026-39836, CVE-2026-42499, CVE-2026-42504, CVE-2026-56858,
CVE-2026-56859, CVE-2026-56860  

**Verdict:** NOT_AFFECTED for each ID above.  

**Mechanism:** OSV symbols require parsing URLs/mail/MIME, DNS helpers,
XML/ASN.1 decode, or `html/template` execute. Not used by gosu’s documented
control flow; govulncheck Module Results only. CVE-2026-39836 additionally
documents Windows NUL/`LookupPort` conditions while this binary is
`GOOS=linux` (supporting detail, not the sole basis).

### Group C — `os.Root` APIs

**ID:** CVE-2026-39822 (GO-2026-4970)  

**Verdict:** NOT_AFFECTED.  

**Mechanism:** Appears in govulncheck **Package Results** (imported `os`
surface) but **not** Symbol Results. Vulnerable symbols are `os.Root` /
`OpenInRoot` family APIs. gosu uses `os.Stat` / env / `os/exec` + `syscall.Exec`,
not `Root.*`. govulncheck still reports 0 called vulnerabilities.

## Limitations

- Static call-graph / binary analysis ≠ runtime exploit demonstration.
- Trivy correctly flags stdlib CVEs present in the **module/binary**; it does
  not perform gosu call-reachability. Disagreement with govulncheck Symbol
  Results is expected and is **not** treated as a Trivy defect.
- This review does **not** approve VEX, change gate policy, or remove findings.
- Postgres was not executed; entrypoint behavior is inferred from upstream
  Dockerfile/entrypoint docs and gosu source.
- FFmpeg / API OS residuals are out of scope (Stage 2).

## Proposed future owner-approved VEX/exception scope (NOT APPLIED)

If ownership later approves exceptions, a minimal scope would be:

| Field | Proposed value |
| --- | --- |
| Advisories | All 22 CVE/GO pairs listed above |
| Component | `/usr/local/bin/gosu` from official gosu **1.19** (`stdlib` gobinary) |
| Artifact | `postgres@sha256:1a66d744c1b459e13b05a8fca341da84cb63383e99ce262210efee5a319d4551` (linux/amd64); binary SHA-256 `52c8749d0142edd234e9d6bd5237dff2d81e71f43537e2f4f66f75dd4b243dd0` |
| Justification | `govulncheck@v1.1.4` Symbol Results empty for each GO-ID on this binary and matching `1.19` source; OSV symbols not on gosu’s call path |
| Evidence pointers | This document; `/tmp/fetchnow-sec03c-gosu-applicability/` govulncheck verbose outputs; parent summary hash above |
| Review triggers | New gosu version; Go rebuild of gosu; new govulncheck/DB hit under **Symbol Results**; Postgres image digest change; entrypoint stops using stock gosu |

Do **not** implement `.trivyignore`, VEX, or gate overrides from this document alone.

## Recommended next action

1. Owner decision: approve a bounded VEX/exception for these 22 IDs on this
   exact gosu/Postgres digest **or** keep gate **fail** until upstream Postgres/
   gosu ships a newer Go build.
2. Do not treat “upstream wait” as risk elimination if no exception is approved.
3. Proceed separately to Stage 2 (API unfixed FFmpeg/OS) — not started here.

## Working evidence (local, disposable)

Directory: `/tmp/fetchnow-sec03c-gosu-applicability/`  
Notable SHA-256:

- gosu binary: `52c8749d0142edd234e9d6bd5237dff2d81e71f43537e2f4f66f75dd4b243dd0`
- govulncheck binary verbose: `f2f383ee6d09e16bb6d9ca3ef5c609c1d6d9d1bbd4deb8ccb4d7bae18f743806`
- govulncheck source verbose: `463baa2def2f2323df911b64e08a8778aee6e5f8df3a3fe53bc191062e8c2607`
- classification.json: `01b1327b04887c47f4906b5860bec71c0d7412043b428bfb736f223569a79e14`
