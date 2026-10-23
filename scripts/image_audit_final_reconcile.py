#!/usr/bin/env python3
"""Offline reconciliation of the final candidate audit with run 38689849543.

Compares Image IDs, vulnerable package versions, HIGH/CRITICAL advisory IDs and
instance rows, fixable/unfixed splits and advisory DB snapshots. It only
reports whether the historical owner approval (47 IDs / 514 rows, bound to the
historical artifacts) would need re-approval; it never extends or applies it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

HISTORICAL_SUMMARY_SHA256 = (
    "163d272b35cf43b06efd6888e2083607b01e4d612a09160e4c270a1db015448f"
)
APPROVED_UNFIXED_IDS = 47
APPROVED_UNFIXED_ROWS = 514

RowKey = tuple[str, str, str, str, str, str]


def _row(row: Mapping[str, Any]) -> RowKey:
    return (
        str(row.get("target")),
        str(row.get("id")),
        str(row.get("package")),
        str(row.get("installed_version")),
        str(row.get("fixed_version") or ""),
        str(row.get("severity")),
    )


def _hc_rows(summary: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    hc = summary.get("high_critical") or {}
    return list(hc.get("fixable_instances") or []) + list(
        hc.get("unfixed_instances") or []
    )


def _identities(summary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for target in summary.get("targets") or []:
        ident = target.get("identity") or {}
        out[str(target.get("name"))] = {
            "image_id": ident.get("image_id"),
            "platform": ident.get("platform"),
            "revision_label": ident.get("revision_label"),
            "repo_digests": ident.get("repo_digests") or [],
            "status": target.get("status"),
        }
    return out


def _multiset_diff(
    old: Sequence[RowKey], new: Sequence[RowKey]
) -> tuple[list[RowKey], list[RowKey]]:
    old_c, new_c = Counter(old), Counter(new)
    added = sorted((new_c - old_c).elements())
    gone = sorted((old_c - new_c).elements())
    return added, gone


def _changed(added: Sequence[RowKey], gone: Sequence[RowKey]) -> list[dict[str, Any]]:
    def by_key(rows: Sequence[RowKey]) -> dict[tuple[str, str, str], list[RowKey]]:
        grouped: dict[tuple[str, str, str], list[RowKey]] = {}
        for r in rows:
            grouped.setdefault((r[0], r[1], r[2]), []).append(r)
        return grouped

    a, g = by_key(added), by_key(gone)
    return [
        {
            "target": key[0],
            "id": key[1],
            "package": key[2],
            "before": [list(r[3:]) for r in g[key]],
            "after": [list(r[3:]) for r in a[key]],
        }
        for key in sorted(set(a) & set(g))
    ]


def reconcile(
    historical: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    h_ids, c_ids = _identities(historical), _identities(current)
    image_ids = {
        name: {
            "historical": h_ids.get(name, {}).get("image_id"),
            "current": c_ids.get(name, {}).get("image_id"),
            "same": h_ids.get(name, {}).get("image_id")
            == c_ids.get(name, {}).get("image_id"),
            "current_platform": c_ids.get(name, {}).get("platform"),
            "current_revision_label": c_ids.get(name, {}).get("revision_label"),
        }
        for name in sorted(set(h_ids) | set(c_ids))
    }
    h_rows = [_row(r) for r in _hc_rows(historical)]
    c_rows = [_row(r) for r in _hc_rows(current)]
    added, gone = _multiset_diff(h_rows, c_rows)
    changed = _changed(added, gone)
    changed_keys = {(c["target"], c["id"], c["package"]) for c in changed}
    new_only = [r for r in added if (r[0], r[1], r[2]) not in changed_keys]
    gone_only = [r for r in gone if (r[0], r[1], r[2]) not in changed_keys]

    def pkgs(summary: Mapping[str, Any]) -> set[tuple[str, str, str]]:
        return {
            (
                str(f.get("target")),
                str(f.get("package")),
                str(f.get("installed_version")),
            )
            for f in summary.get("findings") or []
        }

    h_pk, c_pk = pkgs(historical), pkgs(current)
    h_unfixed = (historical.get("high_critical") or {}).get("unfixed_instances") or []
    c_unfixed = (current.get("high_critical") or {}).get("unfixed_instances") or []
    c_fixable = (current.get("high_critical") or {}).get("fixable_instances") or []
    h_db, c_db = historical.get("db") or {}, current.get("db") or {}
    same_db = bool(h_db.get("metadata_sha256")) and h_db.get(
        "metadata_sha256"
    ) == c_db.get("metadata_sha256")
    unfixed_same_rows = Counter(_row(r) for r in h_unfixed) == Counter(
        _row(r) for r in c_unfixed
    )
    all_images_same = all(v["same"] for v in image_ids.values())
    return {
        "image_ids": image_ids,
        "all_image_ids_same": all_images_same,
        "advisory_db": {
            "historical": h_db,
            "current": c_db,
            "same_db_snapshot": same_db,
            "note": "different DB snapshots: differences mix image and advisory-data changes"
            if not same_db
            else "same DB snapshot",
        },
        "high_critical": {
            "historical_unique_ids": len({r[1] for r in h_rows}),
            "current_unique_ids": len({r[1] for r in c_rows}),
            "historical_instances": len(h_rows),
            "current_instances": len(c_rows),
            "ids_new": sorted({r[1] for r in c_rows} - {r[1] for r in h_rows}),
            "ids_gone": sorted({r[1] for r in h_rows} - {r[1] for r in c_rows}),
            "rows_new": [list(r) for r in new_only],
            "rows_gone": [list(r) for r in gone_only],
            "rows_changed": changed,
        },
        "fixable_current": {
            "instances": len(c_fixable),
            "rows": [list(_row(r)) for r in c_fixable],
        },
        "unfixed": {
            "historical_instances": len(h_unfixed),
            "historical_unique_ids": len({r.get("id") for r in h_unfixed}),
            "current_instances": len(c_unfixed),
            "current_unique_ids": len({r.get("id") for r in c_unfixed}),
            "rows_identical": unfixed_same_rows,
        },
        "vulnerable_packages": {
            "new": sorted(map(list, c_pk - h_pk)),
            "gone": sorted(map(list, h_pk - c_pk)),
        },
        "historical_owner_approval": {
            "scope": f"{APPROVED_UNFIXED_IDS} IDs / {APPROVED_UNFIXED_ROWS} rows on historical Image IDs",
            "historical_scope_matches_summary": len(h_unfixed) == APPROVED_UNFIXED_ROWS
            and len({r.get("id") for r in h_unfixed}) == APPROVED_UNFIXED_IDS,
            "extended_or_applied": False,
            "reapproval_required": not (all_images_same and unfixed_same_rows),
            "reason": "approval is bound to historical artifacts; current Image IDs differ"
            if not all_images_same
            else (
                "unfixed rows differ"
                if not unfixed_same_rows
                else "same artifacts and rows"
            ),
        },
    }


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Final audit reconciliation")
    parser.add_argument("--historical-summary", type=Path, required=True)
    parser.add_argument("--current-summary", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args(argv)
    observed = sha256_path(args.historical_summary)
    if observed != HISTORICAL_SUMMARY_SHA256:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "historical_summary_sha256_mismatch",
                    "observed": observed,
                }
            )
        )
        return 1
    historical = json.loads(args.historical_summary.read_text(encoding="utf-8"))
    current = json.loads(args.current_summary.read_text(encoding="utf-8"))
    result = {
        "ok": True,
        "historical_summary_sha256": observed,
        "current_summary_sha256": sha256_path(args.current_summary),
        **reconcile(historical, current),
    }
    args.json_out.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "ok": True,
                "reapproval_required": result["historical_owner_approval"][
                    "reapproval_required"
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
