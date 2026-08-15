# Threat model (PR0B baseline)

Status: Accepted for PR0B  
Scope: threats relevant before and during introduction of URL fetch, yt-dlp, ffmpeg, jobs, and payments. Mitigations marked **planned** are not all executable in this PR.

Legend for **Planned PR**: documentation/spec = this PR or policy docs; implementation arrives in a later numbered PR when media processing lands.

---

### SSRF via user URL

| Field | Detail |
|---|---|
| Attack | Submit URLs that resolve or redirect to internal HTTP services |
| Impact | Read internal APIs, cloud metadata, or scan private networks |
| Mitigation | Scheme allowlist; block private/link-local/metadata ranges; DNS re-check; redirect re-validation; provider hostname allowlist (`url-validation-policy.md`) |
| Residual risk | Novel IP encodings, shared hosting confused-deputy cases |
| Planned PR | Spec: PR0B; validator: **PR1**; outbound + redirects: **PR2** (ADR 0005) |

### DNS rebinding

| Field | Detail |
|---|---|
| Attack | Hostname resolves to public IP at check time, then to private IP at connect time |
| Impact | Bypass pre-connect SSRF filters |
| Mitigation | Resolve, validate every address; connect-time re-resolve; require non-empty **intersection** of public address sets (CDN-tolerant); no TLS disable / no claimed full IP pinning |
| Residual risk | TOCTOU after connect-time re-resolve during TCP/TLS handshake; intersection ≠ pinning (ADR 0005) |
| Planned PR | **PR2** best-effort intersection; residual risk remains |

### Localhost and private address access

| Field | Detail |
|---|---|
| Attack | `localhost`, `127.0.0.1`, RFC1918, ULA, etc. as destination |
| Impact | Same as SSRF |
| Mitigation | Explicit deny lists for names and resolved addresses (v4+v6) |
| Residual risk | Misconfigured custom allow exceptions in future ops |
| Planned PR | **PR1** + **PR2** redirect hops |

### Wrapper resolution abuse (foundation)

| Field | Detail |
|---|---|
| Attack | Nested wrappers, loops, private redirect targets, HTTP downgrade, arbitrary CDN/manifest candidates |
| Impact | SSRF, resource exhaustion, unsupported media destinations |
| Mitigation | Provider-first classification; sealed exact-host registry with immutable registration snapshots (live `exact_hostnames` ignored after build); HTTPS-only wrapper policy; depth/loop fingerprints; orchestration-scoped document allowlists (initial + redirect hosts; **never** expand allowlist to `yastatic.net` for iframe fetch); Yandex preview exact-shape + preview-ID/`location` binding + dups or trusted `yastatic.net` VK iframe `counters.videoUrl` only (reject embed/`video_ext.php`, lookalike iframe hosts, percent-decode bombs, related-player confusion; no page-wide success scan; safe HTTP→HTTPS string upgrade only) (ADR 0007); typed fail-closed outcomes; error/hop repr redaction ([ADR 0006](../adr/0006-wrapper-resolution-foundation.md), [ADR 0007](../adr/0007-yandex-video-preview-resolver.md)) |
| Residual risk | External Yandex markup drift; first-resolver extraction bugs; iframe fragment/`counters` shape drift |
| Planned PR | Foundation: **PR3A**; Yandex resolver + `/media/resolve`: **PR3B**; metadata: later |

### Yandex iframe fragment / query confusion

| Field | Detail |
|---|---|
| Attack | Query/fragment smuggling, duplicate `counters`, field explosion, JSON depth/node bombs, lookalike player hosts |
| Impact | Wrong provider URL, parser DoS, secret leakage via errors |
| Mitigation | Query forbidden on trusted iframe; strict bounded `parse_qsl`; exactly one `counters`; top-level `videoUrl` only; depth/node limits; no raw fragment/query in logs (ADR 0007) |
| Residual risk | Markup drift of fragment field names |
| Planned PR | **PR3B** hardening |

### IPv4 and IPv6 private ranges

| Field | Detail |
|---|---|
| Attack | Direct literal IPs or DNS answers in private/ULA/mapped ranges |
| Impact | Internal network access |
| Mitigation | Classify and deny all non-public destinations after resolution, including IPv4-mapped IPv6 |
| Residual risk | New special-use ranges over time — keep classifier updated |
| Planned PR | **PR1** (literals + DNS answers) |

### Link-local and metadata endpoints

| Field | Detail |
|---|---|
| Attack | `169.254.169.254`, link-local IPv6, IMDSv1-style targets |
| Impact | Cloud credential theft |
| Mitigation | Hard deny metadata and link-local; prefer instance policies that block IMDS from app network namespace where possible |
| Residual risk | Provider-specific metadata IPs |
| Planned PR | **PR1**/**PR2** + deploy hardening |

### Redirect chains

| Field | Detail |
|---|---|
| Attack | Allowed host redirects to private IP or disallowed host |
| Impact | SSRF / open redirect into internal fetch |
| Mitigation | Manual redirects; re-validate every hop; `URL_MAX_REDIRECTS`; no HTTPS downgrade; same-provider policy |
| Residual risk | Future cross-provider allow rules must stay explicit |
| Planned PR | **PR2** |

### Maliciously large responses

| Field | Detail |
|---|---|
| Attack | Huge Content-Length or endless stream |
| Impact | Disk/memory exhaustion |
| Mitigation | Streamed `OUTBOUND_MAX_RESPONSE_BYTES` on probe; timeouts; reject oversize |
| Residual risk | Full media downloads need separate caps (`MAX_SOURCE_FILE_BYTES`) in media PR |
| Planned PR | Probe caps: **PR2**; media file caps: later |

### Decompression bombs

| Field | Detail |
|---|---|
| Attack | Tiny compressed payload expands hugely |
| Impact | CPU/memory exhaustion |
| Mitigation | Cap decompressed bytes via streamed `OUTBOUND_MAX_RESPONSE_BYTES` on probe; avoid unbounded bodies |
| Residual risk | Media pipeline needs stronger codec-aware limits |
| Planned PR | Probe: **PR2**; media: later |

### Command injection

| Field | Detail |
|---|---|
| Attack | Metacharacters in URL/filename into shell |
| Impact | RCE |
| Mitigation | Never invoke a shell; argv arrays only; no string-built commands |
| Residual risk | Bugs in wrapper libraries |
| Planned PR | yt-dlp/ffmpeg integration PR; enforced by CONTRIBUTING now |

### Filename / path traversal

| Field | Detail |
|---|---|
| Attack | `../`, absolute paths, NUL in suggested filenames |
| Impact | Write/read outside temp root |
| Mitigation | Random storage keys; sanitize display names; chroot-like temp root containment |
| Residual risk | Symlink races if temp dir shared insecurely |
| Planned PR | Storage/lifecycle PR |

### Shell invocation

| Field | Detail |
|---|---|
| Attack | `shell=True` or `/bin/sh -c` with user data |
| Impact | RCE |
| Mitigation | Ban shell invocation for media tools; code review + policy |
| Residual risk | Future contributor mistakes |
| Planned PR | Policy: PR0B; enforcement in integration PR |

### Arbitrary yt-dlp arguments

| Field | Detail |
|---|---|
| Attack | Client-supplied flags (`--exec`, output templates, config paths) |
| Impact | RCE, data exfiltration, SSRF |
| Mitigation | Server-side fixed argv only (`media_inspection`); user supplies URL only; no public endpoint runs the tool inline (PR4) |
| Residual risk | yt-dlp CVEs in allowed code paths; tool performs independent network I/O |
| Planned PR | Enforced in PR4 adapter; worker job wiring in PR5 (still metadata-only) |

### Media job ownership / queue abuse (PR5)

| Field | Detail |
|---|---|
| Attack | Guess job UUID; reuse access token across different URLs; flood enqueue |
| Impact | Unauthorized metadata read; idempotency confusion; capacity exhaustion |
| Mitigation | Client 32-byte token; only domain-separated hashes stored; constant-time compare; unknown/wrong credential → identical `JOB_NOT_FOUND`; `UNIQUE(credential_hash)`; feature default off |
| Residual risk | Application rate limits not enforced in PR5; stolen client token valid until TTL |
| Planned PR | Abuse quotas / gateway limits in later ops PRs |

### Download job / private artifact abuse (PR6)

| Field | Detail |
|---|---|
| Attack | Guess download UUID; steal parent Bearer; flood download enqueue; fill disk with artifacts |
| Impact | Unauthorized status read; capacity/disk exhaustion |
| Mitigation | Parent MediaJob credential authorizes download create/status; UUID alone → identical `DOWNLOAD_JOB_NOT_FOUND`; private artifact root never exposed; `artifactReady` boolean only; `MEDIA_DOWNLOAD_CONCURRENCY` exactly 1; min free disk before claim; runtime output size monitor; feature default off |
| Residual risk | yt-dlp own HTTP outside SafeHTTPClient |
| Planned PR | Stronger isolation / rate limits in later PRs |

### Download cancel / progress oracle (PR10)

| Field | Detail |
|---|---|
| Attack | Guess UUID to cancel someone else's job; distinguish unknown vs unauthorized; leak stage internals or stderr via progress JSON |
| Impact | Denial of another user's download; account oracle; tool/URL leakage |
| Mitigation | Same parent Bearer as GET; UUID alone and wrong credential → identical `DOWNLOAD_JOB_NOT_FOUND`; public progress is an allowlisted stage enum; `progressPercent` is omitted unless a bounded denominator exists; cancel is fenced; stale fence cannot complete a cancelled job or update percent |
| Residual risk | Stolen Bearer until TTL |
| Planned PR | **PR10** |

### Filename / Content-Disposition injection (PR12)

| Field | Detail |
|---|---|
| Attack | Hostile media title injects CR/LF, path separators, or a mismatched `filename*` |
| Impact | Header injection, unexpected save path, confused-deputy download |
| Mitigation | Server-side sanitization persisted at create; RFC 8187 `filename*` must match `suggestedFilename`; ASCII `filename=` must not weaken that check; browser fail-closes and cancels the body on mismatch |
| Residual risk | Stolen Bearer until TTL |
| Planned PR | **PR12** |


### Artifact delivery abuse (PR7)

| Field | Detail |
|---|---|
| Attack | Guess download UUID for content; steal parent Bearer; path/symlink escape; Range abuse; API process reads private store |
| Impact | Unauthorized byte exfiltration; resource exhaustion |
| Mitigation | Dedicated `delivery` service; parent Bearer required; UUID alone → identical `DOWNLOAD_JOB_NOT_FOUND`; FD-relative `O_NOFOLLOW` opens; single bounded Range; API/gateway do not mount artifact root; delivery mount read-only; feature default off; process-local concurrency only |
| Residual risk | Stolen Bearer until TTL; no cross-replica download rate limit; full-file digest not recomputed per GET; shared image still contains yt-dlp (delivery never invokes / no configured path); shared `DATABASE_URL` is not an enforced read-only DB role |

### Browser-native delivery grant abuse (PR14)

| Field | Detail |
|---|---|
| Attack | Steal grant cookie; guess grant UUID; forge cookie; flood grant issuance; use grant after artifact replace |
| Impact | Unauthorized artifact bytes; DoS via grant rows |
| Mitigation | Raw token only in Secure/HttpOnly/SameSite=Strict exact-path cookie; only hash persisted; UUID alone → identical not-found; duplicate cookie rejected; grant bound to artifact_id+fence; TTL `min(config, artifact expiry)`; bounded active grants per job; feature default off; HTTPS required for remote |
| Residual risk | Stolen cookie until grant TTL; embedded webviews that block downloads |
| Planned PR | **PR14** (implemented) |
| Planned PR | Optional offline integrity scans / abuse quotas |

### Media inspection SSRF / extractor escape (PR4)

| Field | Detail |
|---|---|
| Attack | Validated VK/Rutube URL causes yt-dlp to follow redirects, use generic extractor, or emit cross-provider identities |
| Impact | SSRF, unexpected host contact, secret URL leakage |
| Mitigation | Provider host snapshot registry; stable media identity binding (path + authoritative URLs + payload id); `--use-extractors` allowlist without generic; result identity mismatch fail closed; disabled-by-default setting; no public direct/CDN URLs in models/logs |
| Residual risk | yt-dlp’s own HTTP stack is outside SafeHTTPClient DNS double-check while enabled; validate/exec TOCTOU on the trusted binary path |
| Planned PR | Stronger network jail / worker isolation in later ops PRs |

### Malicious media containers

| Field | Detail |
|---|---|
| Attack | Crafted media triggering parser bugs in ffmpeg/yt-dlp |
| Impact | Crash, RCE, resource exhaustion |
| Mitigation | Keep tools patched; run as non-root; cgroup/ulimit; quarantine failures; ffmpeg argv is fixed stream-copy (`-c copy`) with file-only protocol whitelist |
| Residual risk | Zero-days; validate/exec TOCTOU on ffmpeg/ffprobe |
| Planned PR | **PR9** muxing boundary; further jail in later ops PRs |

### ffmpeg resource exhaustion

| Field | Detail |
|---|---|
| Attack | Pathological inputs causing extreme CPU/RAM |
| Impact | Noisy neighbor / DoS |
| Mitigation | Hard timeouts; process-group kill on timeout, cancel, and lease loss during yt-dlp/ffmpeg/ffprobe; bounded stdout/stderr; `MEDIA_DOWNLOAD_CONCURRENCY=1`; duration/size probe after mux; feature default off |
| Residual risk | Uneven demux cost; no transcoding (incompatible pairs fail closed) |
| Planned PR | **PR9** |

### Disk exhaustion

| Field | Detail |
|---|---|
| Attack | Many jobs filling temp volume |
| Impact | Total service outage |
| Mitigation | Soft/hard disk limits; fail closed; TTL cleanup; random keys under quota |
| Residual risk | Cleanup lag under burst |
| Planned PR | Capacity policy now; enforcement in worker PR |

### CPU and memory exhaustion

| Field | Detail |
|---|---|
| Attack | Parallel expensive extracts/transcodes |
| Impact | Host starvation |
| Mitigation | `WORKER_CONCURRENCY`, `METADATA_CONCURRENCY`, process limits |
| Residual risk | Uneven job cost |
| Planned PR | Worker PR |

### Request floods

| Field | Detail |
|---|---|
| Attack | High QPS to create jobs or hit metadata |
| Impact | Cost amplification / outage |
| Mitigation | Future rate limits; capacity errors; gateway timeouts |
| Residual risk | Distributed floods |
| Planned PR | Rate-limit PR (post-MVP foundation) |

### Repeated expensive metadata extraction

| Field | Detail |
|---|---|
| Attack | Resubmit same costly URL |
| Impact | CPU/egress burn |
| Mitigation | Dedup/caching with care; rate limits; concurrency caps |
| Residual risk | Cache poisoning if keyed poorly |
| Planned PR | Job/metadata PR |

### Job duplication

| Field | Detail |
|---|---|
| Attack | Parallel identical jobs |
| Impact | Wasted resources; race on cleanup |
| Mitigation | Idempotency keys / dedupe window in job layer |
| Residual risk | Intentional distinct outputs |
| Planned PR | Job queue PR |

### Stale download URLs

| Field | Detail |
|---|---|
| Attack | Reuse expired or leaked download links |
| Impact | Unauthorized file access |
| Mitigation | Short-lived tokens; bind to job; delete bytes on expiry |
| Residual risk | Token theft within TTL |
| Planned PR | Download delivery PR |

### Leaking source tokens in logs

| Field | Detail |
|---|---|
| Attack | Full URLs with signatures logged |
| Impact | Credential/URL theft via log access |
| Mitigation | Redaction policy (`logging-and-privacy.md`); structured fields only |
| Residual risk | Accidental `print`/debug |
| Planned PR | Policy: PR0B; log helpers in intake PR |

### Webhook spoofing (future)

| Field | Detail |
|---|---|
| Attack | Forged payment/provider webhooks |
| Impact | Fraudulent premium grants |
| Mitigation | Signature verification; replay protection; secrets in env |
| Residual risk | Leaked signing secrets |
| Planned PR | Payments PR |

### Premium token theft (future)

| Field | Detail |
|---|---|
| Attack | Steal Turbo/premium tokens |
| Impact | Unauthorized paid features |
| Mitigation | Short TTL; HttpOnly/secure cookies or one-time tokens; bind to client where practical |
| Residual risk | XSS/malware on client |
| Planned PR | Premium PR |

### Public sharing of prepared files

| Field | Detail |
|---|---|
| Attack | Guessable URLs or web-root exposure |
| Impact | Unintended redistribution; copyright exposure |
| Mitigation | Non-guessable keys; not in public root; short TTL; no indexing; PR7 Bearer on content; no query-token URLs (PR8) |
| Residual risk | User redistributes after download (out of server control) |
| Planned PR | Storage/delivery: **PR7**; browser save: **PR8** |

### Browser credential leakage (PR8)

| Field | Detail |
|---|---|
| Attack | XSS or referrer/history capture of the client access token |
| Impact | Read/create jobs and download artifacts until TTL |
| Mitigation | Token only in `Authorization`; never in URL/DOM/filename; tab-scoped bounded `sessionStorage`; gateway HTML CSP `script-src 'self'; connect-src 'self'` without `unsafe-eval`; same-origin fetch only |
| Residual risk | Same-origin XSS can still read `sessionStorage`; no cross-replica rate limit |
| Planned PR | **PR8** |

### Abuse through unsupported providers

| Field | Detail |
|---|---|
| Attack | Trick service into fetching arbitrary hosts |
| Impact | SSRF, ToS violations, legal risk |
| Mitigation | Strict provider hostname allowlist with boundary checks |
| Residual risk | Compromised allowlisted CDN host |
| Planned PR | Spec: PR0B; allowlist enforcement: **PR1** |

## Trust boundaries (summary)

- Browser/client → gateway/API: untrusted input.
- API → worker/queue: authenticated internal channel; still treat job payloads as untrusted data.
- Worker → internet: only through URL policy + tool wrappers.
- Worker → disk: temp volume only; never public web root.

See [ADR 0003](../adr/0003-security-boundaries-before-media-processing.md).
