# SEC-08 / SEC-09 — isolation architecture (feasibility revision)

Status: **SEC-08 — NATIVE RUN INCOMPLETE / NOT READY FOR IMPLEMENTATION**

Worktree: `/Users/dina/projects/FetchNow-sec03c`
Branch: `codex/security-sec-03c-image-audit`
Harness baseline before this correction: `e84202186af0a719d99b5b8aafde01027b9f0245`
Draft PR: #158

Implementation of the executor, merge, and deploy: **NO**.
One diagnostic harness commit is separate from that.
SEC-03C gosu VEX and the API residual decision document are **preserved**.
API residual acceptance: **NOT GRANTED**.
SEC-09 implementation: **NOT STARTED**.
TEST / LIVE / SEO: unchanged.

POSIX-only handoff is **not** the architecture. Landlock is mandatory for the
filesystem criteria below. Bubblewrap remains a recorded candidate that did
not launch under the tested policy; it is not the shipping mechanism.

---

## 0. What the evidence actually shows

### 0.1 Historical bubblewrap run (2026-10-01 morning)

On-disk tree `/tmp/fetchnow-sec08-bwrap-20261001T100153Z` is no longer
present. The observations below are the preserved session record.

Runtime: Docker Desktop, guest `Linux 7.0.12-linuxkit aarch64`, image
`debian:trixie-slim` + `bubblewrap 0.12.0`. Constraints held: no privileged,
no Docker socket, no added `CAP_SYS_ADMIN`, default seccomp, AppArmor not
disabled, host sysctl unchanged. Primary run also set
`no-new-privileges:true`.

`bwrap` printed:

```text
bwrap: No permissions to create a new namespace, likely because the kernel
does not allow non-privileged user namespaces.
```

That string is bubblewrap’s EPERM text for one tested combination of kernel,
container runtime, and security policy. It does **not** establish that the
kernel was built without user namespaces. The same guest already exposed
`kernel.unprivileged_userns_clone=1` and a large
`user.max_user_namespaces`. A later diagnostic (below) shows
`CONFIG_USER_NS=y`.

The same morning, a Linux named volume with mode `2770` and group
`mediashare` let uid 10002 read attempt A and returned `PermissionError` for
a `0700` sibling and `published/`. That is a single-reader POSIX check. It
does **not** show isolation of two jobs that are group-accessible at the same
time, and it is not an acceptance result.

`/sys/kernel/security/lsm` was absent. Absence of that file does **not**
prove Landlock is absent (securityfs may simply be unmounted).

Neighbor, `/proc`, socket, cancel-tree, and in-jail ffmpeg checks did not
run, because bubblewrap did not start.

### 0.2 New syscall diagnostic (2026-10-01 evening)

Artifacts (outside git): `/tmp/fetchnow-sec08-nsdiag-20261001T1800Z/`

| File | SHA-256 |
| --- | --- |
| `summary.json` | `bef8398ea69d56d548a8842d9e0780c765effe1e87e584bdec3ddd8e32f359ad` |
| `probe.py` | `a26c7c52acd704a7d6810b11b9c06d920fb132bf82b9247a7320a303e3cadc54` |
| `kconfig.txt` | `f06d93ccbfcf50f5ece5f5b76ac5a46053e54676777f974ce6f41b98342e0a38` |
| `fetchnow-sec08-nsdiag-uid0.json` | `6de87e27dc9e59149a5650ae997fc350f30e21fdec840842807013a5bbe3c1d6` |
| `fetchnow-sec08-nsdiag-uid10002.json` | `3a108890689a8ccd37b0c22fd1a10d1856575aa719d7c299333dd5571bf00032` |
| uid0 inspect | `bebb59a9f64a5ad62c548a24e0bb9c2a01fd558e3d02f2c77aa487822586b9ff` |
| uid10002 inspect | `d316fa9b6d0b8694516e27f34367e9292d38216373725cf34e50d3f1590eca47` |

Disposable image `fetchnow-sec08-nsdiag:local` was removed after the run.
No leftover `fetchnow-sec08*` containers.

Each run: `privileged=false`, `security_opt=["no-new-privileges:true"]`,
`cap_add=null`, `cap_drop=null` (Docker default capability set only), default
seccomp, read-only root. Users: `0:0` and `10002:10002`. No profile sweep.

| Fact | uid 0 | uid 10002 |
| --- | --- | --- |
| `NoNewPrivs` | 1 | 1 |
| `Seccomp` / filters | 2 / 1 | 2 / 1 |
| `CapEff` | `00000000a80425fb` | `0000000000000000` |
| `CAP_SYS_ADMIN` in that set | absent | absent |
| `unshare(CLONE_NEWUSER)` | EPERM (1) `permission_denied` | same |
| `unshare(CLONE_NEWNS/NEWPID/NEWNET)` | EPERM (1) | same |
| `landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` syscall 444 | rc **8** (ABI 8, supported) | same |

Kernel config read from `/proc/config.gz` inside that guest:

- `CONFIG_USER_NS=y`
- `CONFIG_SECURITY_LANDLOCK=y`
- `CONFIG_SECCOMP=y` and `CONFIG_SECCOMP_FILTER=y`
- `CONFIG_CGROUPS=y`
- `# CONFIG_SECURITY_APPARMOR is not set`
- `# CONFIG_SECURITY_YAMA is not set`

`kernel.unprivileged_userns_clone=1`.
`kernel.apparmor_restrict_unprivileged_userns` is absent.
`/sys/kernel/security/lsm` is absent.
`/proc/self/attr/current` returns `EINVAL` (no LSM label interface on this guest).

`landlock_create_ruleset` with flags `0` and a NULL attr returned `EFAULT`.
That call shape is not a version query. `EFAULT` means the kernel dereferenced
the attribute pointer, which is consistent with the syscall existing. Landlock
support is classified from the version query, which returned ABI 8.

### 0.3 Cause classification

| Question | Class |
| --- | --- |
| User-namespace syscall missing (`ENOSYS`) | no — `CONFIG_USER_NS=y`, and the failure is `EPERM` |
| Unprivileged userns disabled by `kernel.unprivileged_userns_clone` | no — value is `1` |
| AppArmor denial | not on this guest — AppArmor is not in the kernel config |
| `unshare(CLONE_NEWUSER)` result | **permission_denied** in this container policy |
| Why NEWUSER is `EPERM` while the sysctl allows it (seccomp filter vs another hook) | **UNKNOWN** — seccomp is active (`Seccomp=2`); splitting that would require a weaker profile, which was not done |
| `CLONE_NEWNS` / `CLONE_NEWPID` / `CLONE_NEWNET` `EPERM` | consistent with **no `CAP_SYS_ADMIN`** in the default Docker set; this does not show those namespace types are compiled out |
| Landlock | **supported**, ABI **8**, for both uids, under NNP and default seccomp |

This Desktop/LinuxKit result is not production compatibility proof.

### 0.4 Production inventory

Attempted read-only SSH to `ubuntuuser@195.209.215.87` with
`~/.ssh/fetchnow-prod` (`IdentitiesOnly`, `BatchMode`). Result:
`Permission denied (publickey)`. OpenSSH reported
`no identity pubkey loaded` from that file and `agent contains no identities`.
No kernel, Docker, sysctl, LSM, or container-profile facts were read.
No production container or binary was started. Production capability inventory
is **UNKNOWN**. This attempt is not a sandbox execution proof.

---

## 1. Corrected claims that the previous revision overstated

- Bubblewrap `EPERM` is a denial for the tested kernel/runtime/policy tuple.
- A missing `/sys/kernel/security/lsm` does not decide Landlock. The version
  query does: ABI 8 on this guest.
- Shared-group POSIX handoff does not isolate two jobs whose directories are
  group-accessible together. Mode `0700` on idle siblings is not the control
  for the active pair.
- Closing FDs and omitting a path from an env var does not, by itself, hide
  the executor socket or `/proc`. A mandatory restriction has to deny those
  opens. That restriction was not executed in the bubblewrap run.
- Omitting `HTTP_PROXY` does not stop a media process from connecting to a
  proxy, or to anything else, that the process’s network namespace can route.
- The API residual decision is made on the verified Candidate A evidence
  **before** a production rollout of that candidate. Isolation work does not
  move that decision to after production.

---

## 2. Recommended mechanism (one): C

**Landlock-enforced attempt view + a separate tool uid + Compose network
attachment that gives the tool no bypass route.**

Bubblewrap is not the implementation path while `unshare(CLONE_NEWUSER)`
returns `EPERM` under the allowed policy and the remaining cause is UNKNOWN.
Adding `CAP_SYS_ADMIN`, `privileged`, or `seccomp=unconfined` is not a
proposed fix.

### 2.1 Mandatory kernel / ABI

| Requirement | Fail-closed behavior |
| --- | --- |
| `landlock_create_ruleset` version query succeeds | If `ENOSYS`, `EOPNOTSUPP`, `EPERM`, or ABI below the pinned minimum, the executor does not start the tool |
| Pinned minimum ABI | Set by the native acceptance test to the lowest ABI that supports the ruleset below (handled file rights including read, write, remove, truncate). Desktop observation ABI 8 is above that floor; production ABI is UNKNOWN |
| Tool uid ≠ executor uid | Entrypoint that still holds the default Docker `CAP_SETUID` / `CAP_SETGID` (already in `CapEff` `…a80425fb`) applies Landlock, then `setuid` to the tool uid, then `exec`. The tool uid’s effective set stays empty |
| No `CAP_SYS_ADMIN`, no privileged, no Docker socket | Unchanged |
| cgroup v2 available for the cancel path (`CONFIG_CGROUPS=y` on the tested guest) | If the chosen cancel method cannot be armed, the tool is not started |

Landlock is inherited across `fork` / `exec` and is not removable by the
tool. A new session does not drop it.

### 2.2 Files, processes, control plane

Worker (uid 10001) keeps `DATABASE_URL`, lease, fence, and publish. It never
shares a pid or user namespace requirement with the tool. The worker talks to
the executor over a Unix socket on a dedicated volume. Different containers do
not share loopback; the socket is the control transport for that reason.

Executor daemon uid **10002** has no database credentials. For each operation
it forks a process that:

1. Builds a Landlock ruleset whose only writable path is that attempt
   directory, plus the read-only binary/library paths the tool needs.
2. Does **not** include the sibling attempt, `published/`, `/proc`, or the
   control-socket directory.
3. Calls `landlock_restrict_self`. On failure, the child exits and the job
   fails.
4. `setuid`s to tool uid **10003** and execs the fixed server argv
   (`inspect_metadata`, `download_progressive`, `mux_copy`,
   `ffprobe_validate` only).

Two jobs at once are two processes with two rulesets. Each ruleset names one
attempt. Group bits and a shared media volume may still exist underneath;
they are not the boundary. The acceptance test uses mode `0777` canaries on
the sibling and on `published/` so a POSIX-only success cannot pass.

The tool uid cannot signal the executor uid. The ruleset does not allow the
socket path. The executor uses `close_fds` so the listening socket is not
inherited. Peer credentials on the socket still accept only the worker uid.

`/proc/<executor>/environ` and `/proc/<worker>/…` are outside the ruleset.
Worker `/proc` is also a different container. The tool’s own environment is
the sanitized tool environment only.

Descendants stay in the same Landlock domain. Cancel walks the recorded
child tree and, for a grandchild that called `setsid`, kills via the cgroup
the executor armed for that invocation (`cgroup.kill` or an equivalent
same-uid scan that the test shows still sees the new session). A lost
executor is a transport failure, not a successful publish. A late result with
a stale fence is dropped.

### 2.3 Network (SEC-09 designed, not implemented)

Landlock network rules (available since ABI 4, present in the observed ABI 8)
restrict TCP ports. They do not pin a destination IP. They are not the egress
control.

Egress control is the container’s network attachment, created by Compose:

| Service | Attachment | Who runs there |
| --- | --- | --- |
| `media-offline` | no external interface (`network_mode: none` or an internal network with no peers) | ffmpeg, ffprobe |
| `media-net` | one internal network whose only other peer is `egress-proxy` | yt-dlp |
| `egress-proxy` | that internal network plus the external provider path | SEC-09 policy |
| worker / Postgres | existing `fetchnow` network | not joined to `media-net` or `media-offline` |

A tool in `media-offline` has no route, so it cannot open a provider
connection and cannot use a proxy that is not reachable. A tool in
`media-net` can still connect to the proxy without `HTTP_PROXY` set; that is
expected. It cannot reach Postgres, metadata, link-local, or another Compose
project unless that address is routed, and those routes are not attached.
If the proxy is down, `media-net` has no second route: the operation fails
closed. The proxy itself re-checks each new destination (IPv4, IPv6, mapped
forms) and dials the address it checked. TLS verification stays on. CONNECT
without MITM does not reveal redirects inside TLS; that residual stays
documented and is not called solved.

### 2.4 Why this variant

Option A (keep bubblewrap and only write a native userns test) leaves the
shipping jail on a syscall that returns `EPERM` here, with an UNKNOWN split
between seccomp and another hook. Option B (a container profile change) is
not identified: the denial is not the userns sysctl and not AppArmor on this
guest, and the only profile change that might clear `unshare` is one this
work is not allowed to adopt (`CAP_SYS_ADMIN` or a disabled seccomp filter).
Option C uses the mechanism that already answered the version query under the
required constraints, and it states the extra uid and network attachments the
Landlock ruleset does not provide by itself.

---

## 3. Native acceptance run (2026-10-01, one shot)

One disposable **native Linux** acceptance run. Not Docker Desktop, not the
production host, not an executor implementation, not a proxy implementation.
No privileged, no `CAP_SYS_ADMIN`, no seccomp/AppArmor disable, no host
sysctl writes. Synthetic files only. No real database and no cloud metadata
address.

Pass requires all of:

1. Two simultaneous tools; each reads and writes only its own attempt.
2. Sibling and `published/` canaries are unreadable and unwritable even when
   their POSIX mode is `0777`.
3. Executor secret environment, `/proc/<executor>/environ`, and the control
   socket path are inaccessible; the socket fd is not inherited; the tool uid
   cannot signal the executor pid.
4. A child and a grandchild that called `setsid` still cannot read the
   sibling canary.
5. Cancel/timeout leaves no survivor from that tree, including the new
   session.
6. A forced Landlock failure (or ABI below the pin) does not exec the tool.
7. The offline attachment cannot connect to a fake listener; the net
   attachment cannot connect to a fake internal service that is not the
   proxy; with the proxy peer absent, the net attachment’s connect fails.

That run was executed once and did not finish.

| Item | Value |
| --- | --- |
| Commit | `e84202186af0a719d99b5b8aafde01027b9f0245` |
| Workflow | [sec08-native-feasibility run 36892631777](https://github.com/DmitriiSeitsman/FetchNow/actions/runs/36892631777) |
| Runner | GitHub `ubuntu-24.04`, x86_64, Landlock ABI **7** (that run's floor was 3) |
| Conclusion | **CANCELLED + confirmed isolation failures** |
| Log SHA-256 | `d3fcf367e0c2a4652b5cbc23e57411a8414f1e15abc3545dd8cb4f94d5a64a18` (`/tmp/sec08-feasibility-run.log`) |
| `result.json` | not written; the process was killed before `finally` |

Entrypoint observed: euid 0, then the tool was uid/gid **10003** for real,
effective, and saved ids, supplementary groups empty, capability
effective/permitted/inheritable/bounding empty, `no_new_privs=1`.
`setresuid(0)`, `setresuid(10002)`, `capset`, and a mode-4755 helper all
failed to regain the launcher. Forced ABI and forced ruleset exits did not
exec the tool. Own-attempt read and write worked. Sibling write, truncate,
rename, and link were denied. `/proc/self/environ` and `/proc/1/environ`
were denied. No inherited socket fd. Opening the parent `cgroup.procs` from
the tool returned `EACCES`.

Sibling read, published read, symlink-follow, and the control-socket
`connect` succeeded. Those are confirmed isolation failures of the ruleset
that ran. The launcher had added a read-only Landlock rule on the directory
containing the binary, and that directory was the shared work root
(`/tmp/sec08-feasibility-work`), so the allowed path included both attempts,
`published/`, and the socket. This run is not reclassified as an
infrastructure-only failure.

The process then blocked in the signal probe. Earlier probes had connected
to the listening socket and nothing accepted those connections, so a later
blocking `connect` filled the backlog. The job was cancelled at the 20
minute limit. Because of that cancellation the following were **NOT RUN**:
same-uid signal result, `cgroup.kill` by uid 10002, offline/proxy
reachability, ffmpeg, and ffprobe. The missing checks do not erase the
isolation failures above.

Production behavior of this ruleset remains **UNKNOWN**. SEC-08
implementation is not approved. That run was not retried. A later
diagnostic harness correction does not replace this classification.

---

## 4. Execution map (unchanged product facts)

| Path | Controller | Tool | Network |
| --- | --- | --- | --- |
| Inspection | worker | yt-dlp, fixed argv | yes, via `media-net` + proxy only |
| Progressive download | worker | yt-dlp, fixed argv | same |
| Mux | worker | ffmpeg, file protocols | no (`media-offline`) |
| Verify | worker | ffprobe | no |

Keep server-fixed argv, sanitized tool env, bounded stdio, lease/fence
before publish, and attempt directories
`{MEDIA_DOWNLOAD_TEMP_ROOT}/{job_id}/{attempt}_{fence}/`. Free, Premium,
delivery, and quota semantics stay as they are.

Production media reachability evidence dated **2026-10-01** (prior
read-only session): revision `2d24140434ccd53388e73744dfd276f313e76113`,
deployment `eff53f36-ae69-42f0-99fb-8fe03b739ff0`, api/worker image
`sha256:6d149ce0b0fa1246156ded2d1cca96eb162650ad62fb4c191d85eb8a920403ed`,
`MEDIA_MUXING_ENABLED=true`. That snapshot is not a Landlock measurement.

---

## 5. SEC-08 implementation scope (after the native test passes)

Do not start this scope on the current approval.

| Piece | Likely paths |
| --- | --- |
| Compose services `media-offline` and `media-net` | `compose.yaml`, staging/production overlays, `.env.*.example` |
| Landlock launcher + UDS daemon | `backend/src/fetchnow/media_executor/` |
| Worker client replacing in-process tool spawn | `media_inspection/process.py`, `downloads/process_download.py`, `downloads/executor.py` |
| Flag defaulting to today’s in-process spawn | `core/config.py` |
| Contract check | `scripts/compose_contract_check.py` |

SEC-08 tests: the seven acceptance rows above, plus malformed RPC rejection,
bounded results, fence rejection after re-claim, media fixtures, and current
backend regressions. SEC-08 still does not declare network policy complete;
that is SEC-09. Shipping `media-net` without the proxy peer is not an allowed
intermediate production state.

SEC-09 (not started): the proxy process, destination checks, and the
fail-closed tests on a disposable network with fake internal listeners.

---

## 6. Order vs SEC-03C / PR #158

SEC-03C is not merge-ready. The image gate stays blocking. Uncommitted VEX
and audit files stay in this worktree until the owner asks to commit them:

- `.github/workflows/ci.yml`
- `scripts/image_audit.py`, `scripts/image_audit_vex.py`
- `docs/operations/sec-03c-gosu-*.md` / `.json`, `sec-03c-image-audit.md`
- `docs/operations/sec-03c-api-media-remediation-decision.md`
- `tests/image_audit/test_image_audit_vex_unit.py`
- this plan

The feasibility harness is commit `e842021` on this branch. VEX and the
residual decision are still uncommitted. Sequence from here:

1. Owner commits the SEC-03C VEX/audit work without weakening the gate.
2. API residual decision is taken on the verified Candidate A record
   **before** any production rollout of that image. Isolation does not delay
   or replace that decision, and it does not clear CVE rows.
3. After the native Landlock acceptance test, a SEC-08 branch stacks on the
   SEC-03C tip (or on main only once #158 has merged). SEC-09 stacks on
   SEC-08.
4. Production stays on the current in-process tools until offline and
   proxy-only attachments are both present. Rollback is the flag back to
   in-process spawn.

---

## 7. Corrected harness, one new run

Run 36892631777 stays **CANCELLED + confirmed isolation failures**. It is
not renamed, not rerun, and not treated as an infrastructure-only miss.

The diagnostic harness correction that follows that run:

- trusted executable under `/opt/sec08-trusted/bin`, work tree under
  `/var/tmp/sec08-feasibility-work`, control directory under
  `/opt/sec08-control`;
- no implicit read rule on the binary's parent;
- a refusal before exec if any read-only root covers an attempt,
  `published/`, the control directory, or the work root;
- ABI floor 6, ruleset size 24, `scoped` =
  `LANDLOCK_SCOPE_SIGNAL | LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET`,
  `handled_access_net = 0`; if the kernel rejects that scope, exec is
  refused and there is no weaker fallback;
- pathname socket denial is a DAC contract (owner uid 10002, mode 0600,
  tool uid 10003 not in that group), with the listener accepting; a timeout
  is not a security denial;
- abstract sockets are in the Landlock scope above;
- per-stage atomic `result.json`, internal budget 10 minutes, job limit
  still 20 minutes, step limit 16 minutes;
- cgroup and `ip netns` setup by the root supervisor are deployment
  prerequisites, not proof that a non-root Compose executor can do either,
  and not a Compose network-policy test.

No implementation approval follows from either run. This is not approval to
add `CAP_SYS_ADMIN`, to disable seccomp, or to start the executor. SEC-08 is
not implemented and is not ready to roll out, including if the new run
passes.

The image-audit workflow on the same commit failed separately
([CI run 36892636976](https://github.com/DmitriiSeitsman/FetchNow/actions/runs/36892636976)).
It was not changed here.

---

## 8. Out of scope

- Executor or proxy code, application Dockerfile/Compose edits, production
  changes.
- Merge and deploy. The one diagnostic harness commit is not implementation approval.
- Treating POSIX modes as the job boundary.
- Optional Landlock.
- `CAP_SYS_ADMIN`, privileged mode, Docker socket, disabled seccomp/AppArmor,
  host sysctl edits.
- Image-gate changes, API/FFmpeg VEX application, CVE closure.
- SEC-04…07 and SEC-10…12.
- Free/Premium, delivery, or quota semantic changes.

---

## Status line

**SEC-08 — NATIVE RUN INCOMPLETE / NOT READY FOR IMPLEMENTATION**

Runner ABI 7. Credential drop to uid 10003 held. Per-attempt read denial was
not measured, because the binary directory rule covered the shared work root.
Signal, cgroup cancel, and network checks did not run before the job timeout.
SEC-09: **NOT STARTED**. API residual acceptance: **NOT GRANTED**.
