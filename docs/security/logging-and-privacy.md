# Logging and privacy baseline

Status: Accepted for PR0B

## Principle

Logs exist for operations and abuse response. They must not become a second copy of user secrets or high-value download URLs.

## Forbidden in production logs

Do not write:

- full user source URLs including query tokens or signatures;
- cookies;
- recovery / magic-link / premium tokens;
- payment secrets, card data, provider secret keys;
- raw webhook bodies without redaction;
- direct CDN / prepared-file URLs that grant download access;
- `Authorization` and similar auth headers;
- session tokens;
- unsanitized filenames that embed secrets or full URLs.

Debug overrides that print the above are forbidden in production (`APP_ENV=production`).

## Allowed fields

Safe examples:

- request ID;
- provider ID;
- normalized hostname (no userinfo, no query);
- route / handler name;
- HTTP status;
- duration;
- job ID;
- stable public error code;
- output byte size;
- container/format;
- resolution;
- aggregated counters and histograms.

## URL redaction strategy

When a URL-shaped value must be referenced:

1. Keep **scheme** (`https`).
2. Keep **normalized hostname** (IDNA ASCII, lowercase).
3. Classify **path**:
   - prefer path omission or replacement with `/redacted` when path may contain identifiers;
   - if product metrics need a path class, map to coarse buckets (`/video`, `/clip`, `/unknown`) — never raw IDs if avoidable.
4. **Query string**: delete entirely by default. If a future metric requires a key, use an explicit allowlist of non-sensitive keys only (e.g. `lang`) and drop all others.
5. **Fragment**: always delete.

Example transform:

- input: `https://User:secret@VK.com/video-1?token=abc#clip`
- logged: `https://vk.com/redacted` (or `https://vk.com` + provider id)

## Abuse audit logs

Abuse intake may store normalized hostname and opaque report IDs — not full tokenized URLs. See `docs/product/abuse-and-copyright-process.md`.

## Related documents

- [Threat model](threat-model.md)
- [Product policy](../product/product-policy.md)
