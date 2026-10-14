# SEC-08/09 — residual list after F1–F4 local remediation

This local corrective pass fixed F1–F4 only. The following remain **out of
scope** for this pass and must not be treated as cleared by local tests or by
native run `38466520000`.

## Confirmed P2 defects (not fixed here)

- `protocol.py` `validate_tool_url` normalization branch is a no-op
- `layout.py` existing ctrl directory not re-validated on restart
- Peer UID checked after request body read on the UDS path
- Unbounded *reserved* (non-running) job directories until disk pressure
- `assert_infrastructure_activation_not_config_rollout` only invoked from unit
  tests (allowlist still excludes infra keys)

## Coverage gaps

- Public `recover` CLI / journal media E2E (native matrix uses
  `_execute_accept_target` helper only)
- Bootstrap + media ON failure with live Docker ownership cleanup
  (unit/journal + offline `rollout_release` transaction covered; disposable
  native scenario with real Docker still required after review)
- Incomplete Bugbot file coverage — not an independent clean PASS

## Known residuals / prerequisites

- CONNECT without MITM: no HTTPS URL-path enforcement
- Production `EGRESS_DENY_*` population and host firewall
- Production Landlock ABI ≥ 6 probe
- Approved production `resource_budget` / slice ceilings (distinct from
  disposable executor disk rails)
- Image CVE findings and owner residual decisions (unchanged; no new scans)

## Prior native evidence

Run `38466520000` remains valid evidence for the **previous** candidate under
media OFF bootstrap + later ON transaction cases. After F1–F4 runtime changes,
new Image IDs / audit evidence do **not** yet exist.
