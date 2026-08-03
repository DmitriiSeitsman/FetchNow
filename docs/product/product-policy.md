# FetchNow product policy

Status: Accepted for PR0B  
Audience: product, engineering, operations  
This document is a **product policy**, not legal advice and not a warranty of legal compliance.

## Purpose

FetchNow helps a user retrieve media from a **URL the user provides**. It is a fetch utility, not a content platform.

## Core rules

1. **User-supplied links only.** FetchNow processes URLs pasted or submitted by the user. The service does not crawl the open web for content on the user’s behalf beyond what is required to resolve that specific request.

2. **No content catalog.** FetchNow does not provide a browsable library, recommendations feed, search index of third-party videos, or curated collection of downloadable titles.

3. **No publication or indexing of prepared files.** Temporary outputs exist only to complete the user’s download. They are not published as pages, sitemaps, public directories, or searchable assets.

4. **No permanent media storage.** Prepared files are ephemeral. Retention is bounded by TTL and cleanup policy (`docs/product/file-lifecycle-policy.md`). FetchNow is not a cloud locker or archive.

5. **User responsibility for rights.** The user is responsible for confirming they have the right to download and use the material. Submitting a URL does not grant FetchNow (or the user) additional rights in the underlying work.

6. **Publicly reachable content only (MVP).** The MVP targets content that is publicly accessible without logging into a private account on the user’s behalf. Authenticated/private sources and reuse of the user’s cookies/sessions are out of MVP scope.

7. **No DRM circumvention.** FetchNow must not bypass DRM or other effective technical protection measures. If a source is protected such that lawful retrieval would require circumvention, processing must be refused.

8. **Discretion to refuse.** FetchNow may refuse any URL or provider request for product, safety, abuse, capacity, legal, or operational reasons — including cases where the host is otherwise on a technical allowlist.

9. **Allowlisted platform ≠ blanket permission.** Supporting a provider hostname (for example a planned VK or Rutube integration) means the product *can technically attempt* fetches for URLs on that platform under policy controls. It does **not** mean every item hosted there may be downloaded, redistributed, or stored.

10. **No absolute copyright compliance claims.** Product and marketing copy must not present FetchNow as providing absolute legal protection, guaranteed copyright compliance, or indemnity. Rights compliance statements must stay accurate and non-absolute.

## What this policy is not

- Not a Terms of Service substitute (ToS may reference this policy later).
- Not a copyright opinion for a specific jurisdiction.
- Not permission to ignore takedown or abuse processes (`docs/product/abuse-and-copyright-process.md`).

## Related documents

- [MVP scope](mvp-scope.md)
- [Abuse and copyright process](abuse-and-copyright-process.md)
- [File lifecycle policy](file-lifecycle-policy.md)
- [Threat model](../security/threat-model.md)
