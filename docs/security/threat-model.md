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
| Planned PR | Spec: PR0B; executable validator: media-intake PR |

### DNS rebinding

| Field | Detail |
|---|---|
| Attack | Hostname resolves to public IP at check time, then to private IP at connect time |
| Impact | Bypass pre-connect SSRF filters |
| Mitigation | Resolve, validate every address, connect to pinned address or re-validate immediately before connect; short TTL awareness; no long-lived “trusted hostname” cache without re-check |
| Residual risk | TOCTOU on some OS resolvers |
| Planned PR | Media-intake / outbound HTTP client PR |

### Localhost and private address access

| Field | Detail |
|---|---|
| Attack | `localhost`, `127.0.0.1`, RFC1918, ULA, etc. as destination |
| Impact | Same as SSRF |
| Mitigation | Explicit deny lists for names and resolved addresses (v4+v6) |
| Residual risk | Misconfigured custom allow exceptions in future ops |
| Planned PR | Media-intake PR |

### IPv4 and IPv6 private ranges

| Field | Detail |
|---|---|
| Attack | Direct literal IPs or DNS answers in private/ULA/mapped ranges |
| Impact | Internal network access |
| Mitigation | Classify and deny all non-public destinations after resolution, including IPv4-mapped IPv6 |
| Residual risk | New special-use ranges over time — keep classifier updated |
| Planned PR | Media-intake PR |

### Link-local and metadata endpoints

| Field | Detail |
|---|---|
| Attack | `169.254.169.254`, link-local IPv6, IMDSv1-style targets |
| Impact | Cloud credential theft |
| Mitigation | Hard deny metadata and link-local; prefer instance policies that block IMDS from app network namespace where possible |
| Residual risk | Provider-specific metadata IPs |
| Planned PR | Media-intake + deploy hardening PR |

### Redirect chains

| Field | Detail |
|---|---|
| Attack | Allowed host redirects to private IP or disallowed host |
| Impact | SSRF / open redirect into internal fetch |
| Mitigation | Re-validate every hop; `MAX_REDIRECTS`; fail closed |
| Residual risk | Protocol downgrade tricks if http allowed carelessly |
| Planned PR | Media-intake PR |

### Maliciously large responses

| Field | Detail |
|---|---|
| Attack | Huge Content-Length or endless stream |
| Impact | Disk/memory exhaustion |
| Mitigation | `MAX_SOURCE_FILE_BYTES` / read caps; timeouts; reject oversize early |
| Residual risk | Lying Content-Length with chunked abuse — enforce byte counters |
| Planned PR | Media pipeline PR |

### Decompression bombs

| Field | Detail |
|---|---|
| Attack | Tiny compressed payload expands hugely |
| Impact | CPU/memory exhaustion |
| Mitigation | Cap decompressed size; avoid unbounded auto-decompress of untrusted bodies |
| Residual risk | Novel codecs |
| Planned PR | Media pipeline PR |

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
| Mitigation | Server-side fixed allowlist of flags; user supplies URL only |
| Residual risk | yt-dlp CVEs in allowed code paths |
| Planned PR | Provider/yt-dlp PR |

### Malicious media containers

| Field | Detail |
|---|---|
| Attack | Crafted media triggering parser bugs in ffmpeg/yt-dlp |
| Impact | Crash, RCE, resource exhaustion |
| Mitigation | Keep tools patched; run as non-root; cgroup/ulimit; quarantine failures |
| Residual risk | Zero-days |
| Planned PR | Media pipeline + ops hardening |

### ffmpeg resource exhaustion

| Field | Detail |
|---|---|
| Attack | Pathological inputs causing extreme CPU/RAM |
| Impact | Noisy neighbor / DoS |
| Mitigation | Timeouts; concurrency caps; duration/byte limits; kill process on exceed |
| Residual risk | Uneven cost per codec |
| Planned PR | Media pipeline PR |

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
| Mitigation | Non-guessable keys; not in public root; short TTL; no indexing |
| Residual risk | User redistributes after download (out of server control) |
| Planned PR | Storage/delivery PR |

### Abuse through unsupported providers

| Field | Detail |
|---|---|
| Attack | Trick service into fetching arbitrary hosts |
| Impact | SSRF, ToS violations, legal risk |
| Mitigation | Strict provider hostname allowlist with boundary checks |
| Residual risk | Compromised allowlisted CDN host |
| Planned PR | Spec: PR0B; allowlist enforcement: provider PR |

## Trust boundaries (summary)

- Browser/client → gateway/API: untrusted input.
- API → worker/queue: authenticated internal channel; still treat job payloads as untrusted data.
- Worker → internet: only through URL policy + tool wrappers.
- Worker → disk: temp volume only; never public web root.

See [ADR 0003](../adr/0003-security-boundaries-before-media-processing.md).
