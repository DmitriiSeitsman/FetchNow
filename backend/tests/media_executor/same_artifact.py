"""Build once; native acceptance and Trivy audit share the same Image ID.

No second build, no registry pull between stages, no mutable-tag refresh.
Cleanup always removes the disposable local image after both stages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PATTERN = re.compile(r"fetchnow-sec08-same-[0-9a-f]{8}\Z")

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import executor_image_audit as audit  # noqa: E402
import native_acceptance as native  # noqa: E402

SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import image_audit as ia  # type: ignore[import-not-found]  # noqa: E402


def save(path: Path, payload: object) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temp.chmod(0o644)
    temp.replace(path)


def inspect_image(image_ref: str) -> dict[str, str]:
    image_id = native.inspect_image_id(image_ref)
    arch = native.run(
        ["docker", "image", "inspect", "--format", "{{.Architecture}}", image_ref]
    ).stdout.strip()
    os_name = native.run(
        ["docker", "image", "inspect", "--format", "{{.Os}}", image_ref]
    ).stdout.strip()
    if arch != "amd64" or os_name != "linux":
        raise OSError(f"unexpected platform {os_name}/{arch}")
    return {
        "image_id": image_id,
        "platform": f"{os_name}/{arch}",
        "image_ref": image_ref,
    }


def build_once(image_ref: str) -> dict[str, str]:
    native.run(
        [
            "docker",
            "build",
            "-f",
            "backend/Dockerfile.media-executor",
            "-t",
            image_ref,
            "backend",
        ],
        timeout=600,
    )
    return inspect_image(image_ref)


def remove_image(image_ref: str) -> list[str]:
    failures: list[str] = []
    try:
        present = native.run(
            ["docker", "image", "ls", "-q", image_ref], check=False
        ).stdout.strip()
        if present:
            native.run(["docker", "image", "rm", "-f", image_ref])
    except (OSError, subprocess.TimeoutExpired):
        failures.append("image")
    return failures


def dockerfile_sha256() -> str:
    path = ROOT / "backend" / "Dockerfile.media-executor"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_native_stage(
    *,
    output: Path,
    image_ref: str,
    expected_image_id: str,
) -> dict[str, Any]:
    native_dir = output / "native"
    code = native.execute(
        native_dir,
        image=image_ref,
        expected_image_id=expected_image_id,
        retain_image=True,
    )
    result_path = native_dir / "result.json"
    payload = json.loads(result_path.read_text()) if result_path.exists() else {}
    observed = inspect_image(image_ref)
    if observed["image_id"] != expected_image_id:
        raise OSError(
            "image identity drift after native: "
            f"{observed['image_id']} != {expected_image_id}"
        )
    return {
        "exit_code": code,
        "status": payload.get("status", "FAIL"),
        "result": payload,
        "identity": observed,
    }


def run_audit_stage(
    *,
    output: Path,
    image_ref: str,
    expected_image_id: str,
    expected_platform: str,
    trivy_bin: str,
) -> dict[str, Any]:
    observed = inspect_image(image_ref)
    if observed["image_id"] != expected_image_id:
        raise OSError(
            "image identity drift before audit: "
            f"{observed['image_id']} != {expected_image_id}"
        )
    if observed["platform"] != expected_platform:
        raise OSError(
            "platform drift before audit: "
            f"{observed['platform']} != {expected_platform}"
        )
    audit_dir = output / "audit"
    summary_out = audit_dir / "summary.json"
    summary, code = audit.audit_image(
        image_ref=image_ref,
        expected_image_id=expected_image_id,
        trivy_bin=trivy_bin,
        cache_dir=audit_dir / "trivy-cache",
        work_dir=audit_dir / "work",
        expected_platform=expected_platform,
    )
    summary_out.parent.mkdir(parents=True, exist_ok=True)
    summary_out.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    after = inspect_image(image_ref)
    if after["image_id"] != expected_image_id:
        raise OSError(
            "image identity drift after audit: "
            f"{after['image_id']} != {expected_image_id}"
        )
    return {
        "exit_code": code,
        "status": summary.get("status"),
        "summary": summary,
        "identity": after,
        "summary_sha256": hashlib.sha256(summary_out.read_bytes()).hexdigest(),
        "raw_report_sha256": (
            hashlib.sha256(
                (audit_dir / "work" / "media-executor.trivy.json").read_bytes()
            ).hexdigest()
            if (audit_dir / "work" / "media-executor.trivy.json").is_file()
            else None
        ),
    }


def combine_exit(
    *,
    native_status: str,
    audit_status: str | None,
    technical: bool,
) -> tuple[str, int]:
    if technical:
        return "technical_failure", ia.EXIT_FAIL
    if native_status != "PASS":
        return "native_fail", ia.EXIT_FAIL
    if audit_status == ia.STATUS_TECHNICAL:
        return "audit_technical_failure", ia.EXIT_FAIL
    if audit_status == ia.STATUS_FAIL:
        return "fixable_findings", ia.EXIT_FAIL
    if audit_status == ia.STATUS_NEEDS_OWNER:
        return "unfixed_residual", ia.EXIT_NEEDS_OWNER
    if audit_status == ia.STATUS_PASS:
        return "pass", ia.EXIT_PASS
    return "technical_failure", ia.EXIT_FAIL


def execute(
    output: Path,
    *,
    trivy_bin: str,
    image_ref: str | None = None,
) -> int:
    if output.exists():
        raise ValueError("output directory must not exist")
    output.mkdir(parents=True, exist_ok=False)
    name = f"fetchnow-sec08-same-{uuid.uuid4().hex[:8]}"
    ref = image_ref or f"fetchnow-media-executor:{name}"
    state: dict[str, Any] = {"name": name, "image_ref": ref}
    save(output / "state.json", state)
    result: dict[str, Any] = {
        "status": "FAIL",
        "phase": "preflight",
        "native_verdict": None,
        "audit_verdict": None,
        "same_artifact": True,
        "rebuild_between_stages": False,
    }
    technical = False
    image_built = False
    try:
        assert os.geteuid() == 0
        assert platform.system() == "Linux" and platform.machine() == "x86_64"
        result["phase"] = "build"
        identity = build_once(ref)
        image_built = True
        state["image_id"] = identity["image_id"]
        state["platform"] = identity["platform"]
        save(output / "state.json", state)
        result["identity"] = identity
        result["dockerfile_sha256"] = dockerfile_sha256()
        result["source_commit"] = os.environ.get("GITHUB_SHA") or native.run(
            ["git", "rev-parse", "HEAD"]
        ).stdout.strip()

        result["phase"] = "native"
        native_stage = run_native_stage(
            output=output,
            image_ref=ref,
            expected_image_id=identity["image_id"],
        )
        result["native_verdict"] = native_stage["status"]
        result["native_exit_code"] = native_stage["exit_code"]
        result["native_identity"] = native_stage["identity"]
        result["native_image_id"] = native_stage["identity"]["image_id"]
        if native_stage["identity"]["image_id"] != identity["image_id"]:
            raise OSError("native identity mismatch")

        result["phase"] = "audit"
        audit_stage = run_audit_stage(
            output=output,
            image_ref=ref,
            expected_image_id=identity["image_id"],
            expected_platform=identity["platform"],
            trivy_bin=trivy_bin,
        )
        result["audit_verdict"] = audit_stage["status"]
        result["audit_exit_code"] = audit_stage["exit_code"]
        result["audit_identity"] = audit_stage["identity"]
        result["audit_image_id"] = audit_stage["identity"]["image_id"]
        result["audit_summary_sha256"] = audit_stage["summary_sha256"]
        result["audit_raw_report_sha256"] = audit_stage["raw_report_sha256"]
        result["cve_2026_103111_present"] = (audit_stage["summary"] or {}).get(
            "cve_2026_103111_present"
        )
        result["gate_fixable_unique_ids"] = (audit_stage["summary"] or {}).get(
            "gate_fixable_unique_ids"
        )
        result["gate_unfixed_unique_ids"] = (audit_stage["summary"] or {}).get(
            "gate_unfixed_unique_ids"
        )
        result["gate_fixable_instances"] = (audit_stage["summary"] or {}).get(
            "gate_fixable_instances"
        )
        result["gate_unfixed_instances"] = (audit_stage["summary"] or {}).get(
            "gate_unfixed_instances"
        )
        result["db"] = (audit_stage["summary"] or {}).get("db")
        result["scanner"] = (audit_stage["summary"] or {}).get("scanner")
        if audit_stage["identity"]["image_id"] != identity["image_id"]:
            raise OSError("audit identity mismatch")
    except Exception as exc:
        technical = True
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)[:500]
        print(f"same-artifact failed in {result['phase']}: {exc}", flush=True)
    finally:
        cleanup_errors: list[str] = []
        if image_built:
            cleanup_errors.extend(remove_image(ref))
        # Native retain leaves containers/slice cleaned by native.execute finally;
        # re-run scoped cleanup without image if state exists.
        native_state = output / "native" / "state.json"
        if native_state.exists():
            try:
                cleanup_errors.extend(
                    native.cleanup(
                        json.loads(native_state.read_text()), remove_image=False
                    )
                )
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        # Drop raw trivy JSON from the retained tree; keep hashes in result.
        raw = output / "audit" / "work" / "media-executor.trivy.json"
        if raw.is_file():
            try:
                raw.unlink()
            except OSError:
                cleanup_errors.append("raw-report")
        cache = output / "audit" / "trivy-cache"
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
        result["cleanup_errors"] = cleanup_errors
        overall, code = combine_exit(
            native_status=str(result.get("native_verdict") or "FAIL"),
            audit_status=(
                str(result["audit_verdict"])
                if result.get("audit_verdict") is not None
                else None
            ),
            technical=technical or bool(cleanup_errors),
        )
        result["status"] = overall
        result["overall_exit_code"] = code
        save(output / "result.json", result)
        save(
            output / "hashes.json",
            {
                "result.json": hashlib.sha256(
                    (output / "result.json").read_bytes()
                ).hexdigest()
            },
        )
    return int(result["overall_exit_code"])


def cleanup_only(output: Path) -> int:
    errors: list[str] = []
    state_path = output / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        name = state.get("name")
        ref = state.get("image_ref")
        if isinstance(name, str) and not PATTERN.fullmatch(name):
            raise ValueError("unsafe cleanup identity")
        if isinstance(ref, str):
            errors.extend(remove_image(ref))
    native_state = output / "native" / "state.json"
    if native_state.exists():
        errors.extend(
            native.cleanup(json.loads(native_state.read_text()), remove_image=True)
        )
    return 1 if errors else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trivy-bin", required=True)
    parser.add_argument("--image-ref", default=None)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if args.cleanup:
        raise SystemExit(cleanup_only(args.output))
    raise SystemExit(
        execute(args.output, trivy_bin=args.trivy_bin, image_ref=args.image_ref)
    )
