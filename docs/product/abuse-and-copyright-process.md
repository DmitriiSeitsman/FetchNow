# Abuse and copyright process

Status: Accepted for PR0B  
This describes an **operational intake process**. It is not a legally binding SLA and not legal advice.

## Contact placeholder

Abuse and rights communications use a configured address. Do **not** hardcode a production mailbox in application code or docs.

```env
ABUSE_CONTACT_EMAIL=
```

Until a real mailbox is provisioned, leave the variable empty in examples. Runtime surfaces that need a contact string must read it from environment/config and degrade gracefully when unset (show “contact not configured” to operators, not a fake email).

## What we accept

Reports concerning FetchNow’s processing of third-party material, including:

- alleged copyright infringement related to a fetch the service performed or attempted;
- abuse of the service (malware links, spam floods, attempts to probe internal networks);
- urgent safety reports tied to a specific URL or provider pattern.

We do **not** use this channel as a general product support inbox.

## Minimum information from the complainant

A usable report should include:

1. **Complainant identity** — name or organization, and a reliable reply contact.
2. **Source URL** — the original media URL (or closest stable locator) at issue.
3. **Authority** — statement and supporting evidence that the complainant is the rights holder or an authorized representative.
4. **Right asserted** — short description of the right claimed to be infringed (e.g., copyright in a specific work) and why FetchNow’s processing is implicated.
5. **FetchNow context if known** — approximate time, job/request identifiers if the complainant has them, provider name.
6. **Requested action** — disable URL, pattern, provider, or other clear ask.

Incomplete reports may be acknowledged and held until minimum fields are supplied.

## Operator actions

Upon a plausible report, operators may:

1. Acknowledge receipt (when contact is configured).
2. Locate related jobs/logs using **redacted** identifiers only (`docs/security/logging-and-privacy.md`).
3. **Temporarily disable** processing for:
   - a specific normalized URL or URL fingerprint;
   - a hostname/path pattern;
   - an entire provider integration.
4. Delete or quarantine remaining temporary artifacts for matching jobs when present.
5. Record a **minimal audit entry** of the report and action taken (see below).
6. Reply with the outcome at a high level (action taken / need more info / not applicable). No public docket of removals is published.

## Temporary disables

Disables are operational controls, not a public “removed content” library:

- Prefer the narrowest effective scope (URL → pattern → provider → domain).
- Document who enabled the disable, why, and how to review/lift it.
- There is **no** public searchable catalog of disabled URLs or takedown history.

## Minimal audit retention

Store only what is needed to show that a report was received and acted on, for example:

- opaque report ID;
- received-at timestamp;
- normalized source hostname (not full URL with tokens);
- asserted right category;
- action taken (none / URL block / pattern block / provider disable);
- operator identity or role;
- reply-sent flag.

Do **not** retain unpaid payment secrets, cookies, or full source URLs with query tokens in the audit trail.

## Review timing

Internal target: begin review of complete reports within a small number of business days. This is an **operational aspiration**, not a guaranteed or legally binding SLA. Capacity, holidays, and incomplete reports affect timing.

## Related documents

- [Product policy](product-policy.md)
- [Logging and privacy](../security/logging-and-privacy.md)
- [URL validation policy](../security/url-validation-policy.md)
