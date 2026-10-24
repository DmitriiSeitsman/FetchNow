#!/usr/bin/env python3
"""Native saved-image export/import acceptance (disposable; not a security PASS).

Two explicit phases on separate native linux/amd64 Docker daemons:

* ``builder``: standard ``prepare`` build (media mode) of the pinned product
  source, explicit candidate PostgreSQL by registry digest, ``export-images``.
* ``importer``: standard ``prepare --image-bundle`` on a clean daemon, refusal
  cases, idempotency and identity checks. No build, pull or activation.

Trusted-main authority is a TEST FIXTURE: in a disposable local clone,
``refs/remotes/origin/main`` is set to the pinned product source. This is not
production origin/main trust and production tooling gains no bypass.

New artifacts only: no historical Image ID, native PASS, VEX or approval
transfers. NOT_RUN exits 2, FAIL exits 1, PASS exits 0. No retries, no prune.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(ROOT / "tests" / "release") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests" / "release"))

EXECUTE_ENV = "FETCHNOW_NATIVE_SAVED_IMAGE_DELIVERY_EXECUTE"
PRODUCT_SHA = "3792c377ecde1218ad9da9f3d5b91d620ed63b46"
PRODUCT_TREE = "e9dbbc4588c2e968fd32e0201dac99b075924134"
PRODUCT_PARENT = "5dfd0524758c7ae55e04cc7045f709c6cb22c66f"
TOOLING_PARENT = "97b3964160fb6757dd6e86d497f1cd3d838754be"
RELEASE_MODE = "media"
OVERLAY = "compose.staging.yaml"
PLATFORM = "linux/amd64"
POSTGRES_REPOSITORY = "postgres"
POSTGRES_CANDIDATE = {
    "candidate_only": True,
    "platform": PLATFORM,
    "source": "SEC-08/09 final candidate audit 40070427618 postgres-identity.json",
    "config_image_id": (
        "sha256:6a2e324ebe59af6031c4c1cd96c1d5072c2ebc36563d2196365e43b8297c062d"
    ),
    "registry_index_digest": (
        "sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36"
    ),
    "platform_manifest_digest_linux_amd64": (
        "sha256:cbf42f0edc946c0bc1b8eeed62403734e1f25dc67a3491455d07a92fdb558b28"
    ),
}
FIXTURE_AUTHORITY = (
    "TEST_FIXTURE: disposable local clone refs/remotes/origin/main := product "
    "source SHA; not production origin/main trust"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PREPARE_BUILD_TIMEOUT = 3600.0
EXPORT_TIMEOUT = 1800.0
IMPORT_TIMEOUT = 1800.0
SHORT_TIMEOUT = 120.0
OUTPUT_LIMIT = 1024 * 1024
IMPORT_IMAGE_ACTIONS = frozenset({"load", "tag"})
# Exact production messages (offline test asserts they exist in the sources).
MARK_BUILT = "application images built and inspected"
MARK_IMPORTED = "exact saved images imported; no build/pull performed"
MARK_PUBLISHED = "OK: release published atomically"
MARK_IDEMPOTENT = "OK: release already prepared"
REASON_CHECKSUM = "bundle manifest checksum mismatch"
REASON_ARCHIVE = "archive checksum/size mismatch: "
REASON_FILES = "bundle missing files or contains extra files"
REASON_REVISION_MODE = "bundle revision/release_mode differs"
REASON_OVERLAY = "bundle overlay differs from target"
REASON_TAG = "refusing to retarget existing image tag"
REASON_RECEIPT = "lacks matching bundle import receipt"
OS_MISSING_FILE = "No such file"


class AcceptanceError(RuntimeError):
    """A required acceptance condition failed (never retried)."""


# --------------------------------------------------------------------------
# Pure helpers (offline-tested)
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_inventory(lines: str) -> dict[str, Any]:
    """Parse ``docker image ls --all --no-trunc --format '{{json .}}'``."""
    tags: dict[str, str] = {}
    ids: set[str] = set()
    for line in lines.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        image_id = row.get("ID")
        if not isinstance(image_id, str) or not IMAGE_ID_RE.fullmatch(image_id):
            raise AcceptanceError("invalid Docker image inventory row")
        ids.add(image_id)
        repo, tag = row.get("Repository"), row.get("Tag")
        if isinstance(repo, str) and isinstance(tag, str):
            if repo != "<none>" and tag != "<none>":
                ref = f"{repo}:{tag}"
                if ref in tags and tags[ref] != image_id:
                    raise AcceptanceError("conflicting Docker tag inventory")
                tags[ref] = image_id
    return {"tags": dict(sorted(tags.items())), "ids": sorted(ids)}


def inventory_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    b_tags, a_tags = dict(before["tags"]), dict(after["tags"])
    b_ids, a_ids = set(before["ids"]), set(after["ids"])
    return {
        "new_ids": sorted(a_ids - b_ids),
        "removed_ids": sorted(b_ids - a_ids),
        "new_tags": {k: v for k, v in sorted(a_tags.items()) if k not in b_tags},
        "removed_tags": sorted(k for k in b_tags if k not in a_tags),
        "retargeted_tags": sorted(
            k for k in b_tags if k in a_tags and a_tags[k] != b_tags[k]
        ),
    }


def parse_events(lines: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines.splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        actor = raw.get("Actor") if isinstance(raw.get("Actor"), dict) else {}
        attributes = actor.get("Attributes") if isinstance(actor, dict) else {}
        events.append(
            {
                "type": str(raw.get("Type") or raw.get("type") or ""),
                "action": str(raw.get("Action") or raw.get("status") or ""),
                "actor_id": str(actor.get("ID") or raw.get("id") or ""),
                "name": str((attributes or {}).get("name") or ""),
                "time_nano": int(raw.get("timeNano") or 0),
            }
        )
    return events


def event_violations(
    events: list[dict[str, Any]], *, allowed_image_actions: frozenset[str]
) -> list[dict[str, Any]]:
    """Only listed image actions are permitted; pull/build/container are never."""
    bad: list[dict[str, Any]] = []
    for event in events:
        if event["type"] == "image" and event["action"] in allowed_image_actions:
            continue
        bad.append(event)
    return bad


def parse_build_cache_count(lines: str) -> int:
    """``docker system df --format '{{json .}}'`` Build Cache TotalCount."""
    for line in lines.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if str(row.get("Type", "")).lower() == "build cache":
            return int(str(row.get("TotalCount", "0")).strip() or 0)
    raise AcceptanceError("build cache row missing from docker system df")


def link_copy(
    src: Path,
    dst: Path,
    *,
    real_copy: tuple[str, ...] = (),
    omit: tuple[str, ...] = (),
) -> None:
    """Separate case directory: hardlink unchanged files, copy files to mutate."""
    dst.mkdir(mode=0o700, parents=True, exist_ok=False)
    for path in sorted(src.iterdir()):
        if path.name in omit:
            continue
        if path.is_symlink() or not path.is_file():
            raise AcceptanceError("bundle contains a non-regular file")
        target = dst / path.name
        if path.name in real_copy:
            shutil.copyfile(path, target)
        else:
            os.link(path, target)


def flip_byte(path: Path, offset: int) -> None:
    with path.open("r+b") as fh:
        fh.seek(offset)
        value = fh.read(1)
        if len(value) != 1:
            raise AcceptanceError("corruption offset outside archive")
        fh.seek(offset)
        fh.write(bytes([value[0] ^ 0xFF]))
        fh.flush()
        os.fsync(fh.fileno())


def directory_digest(root: Path) -> dict[str, str]:
    """Relative path -> sha256 / 'dir' / 'symlink:<target>' (never follows)."""
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in sorted(dirnames):
            path = base / name
            rel = str(path.relative_to(root))
            result[rel] = "symlink:" + os.readlink(path) if path.is_symlink() else "dir"
        for name in sorted(filenames):
            path = base / name
            rel = str(path.relative_to(root))
            result[rel] = (
                "symlink:" + os.readlink(path)
                if path.is_symlink()
                else sha256_file(path)
            )
    return dict(sorted(result.items()))


def bundle_file_hashes(bundle: Path) -> dict[str, str]:
    return {p.name: sha256_file(p) for p in sorted(bundle.iterdir()) if p.is_file()}


def published_release_state(deploy: Path, revision: str) -> dict[str, Any]:
    from fetchnow_release.current_state import current_path
    from fetchnow_release.media_state import media_state_path

    releases = deploy / "releases"
    return {
        "release_published": (releases / revision).exists(),
        "incomplete_entries": sorted(
            p.name for p in deploy.iterdir() if p.name.startswith(".incomplete-")
        )
        if deploy.exists()
        else [],
        "current_present": current_path(deploy).exists(),
        "media_authority_present": media_state_path(deploy).exists(),
    }


def compare_builder_importer(
    builder_images: dict[str, dict[str, Any]],
    importer_images: dict[str, dict[str, Any]],
) -> list[str]:
    """Return mismatches between builder and importer image identities."""
    problems: list[str] = []
    if set(builder_images) != set(importer_images):
        problems.append("service set differs")
    for svc in sorted(set(builder_images) & set(importer_images)):
        b, i = builder_images[svc], importer_images[svc]
        for key in ("image_id", "platform", "revision_label"):
            if b.get(key) != i.get(key):
                problems.append(f"{svc}:{key}")
    return problems


def privacy_hits(evidence: Path, secrets_to_find: tuple[str, ...]) -> list[str]:
    patterns = [
        re.compile(rb"gh[pousr]_[A-Za-z0-9]{36,}"),
        re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(rb"(?i)authorization:\s*bearer\s+[A-Za-z0-9._~+/=-]{20,}"),
        re.compile(rb"POSTGRES_PASSWORD=(?!<redacted>)[^\s\"]"),
    ]
    needles = [s.encode() for s in secrets_to_find if len(s) >= 8]
    hits: list[str] = []
    for path in sorted(evidence.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if any(p.search(data) for p in patterns) or any(n in data for n in needles):
            hits.append(str(path.relative_to(evidence)))
    return hits


# --------------------------------------------------------------------------
# Evidence (written stage by stage; primary error kept separately)
# --------------------------------------------------------------------------


class Evidence:
    def __init__(self, directory: Path, phase: str) -> None:
        self.dir = directory
        self.dir.mkdir(mode=0o750, parents=False, exist_ok=False)
        self.phase = phase
        self.counter = 0
        self.cases: list[dict[str, Any]] = []
        self.primary_error: str | None = None
        self.summary: dict[str, Any] = {}

    def write(self, name: str, payload: object) -> Path:
        path = self.dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
        return path

    def stage(self, name: str, payload: object) -> None:
        self.counter += 1
        self.write(f"stage-{self.counter:02d}-{name}.json", payload)

    def case(self, name: str, ok: bool, **details: object) -> None:
        entry: dict[str, Any] = {"name": name, "status": "PASS" if ok else "FAIL"}
        entry.update(details)
        self.cases.append(entry)
        self.write("cases.json", self.cases)
        if not ok:
            raise AcceptanceError(f"case failed: {name}")

    def finish(self, status: str, planned: tuple[str, ...]) -> None:
        seen = {c["name"] for c in self.cases}
        for name in planned:
            if name not in seen:
                self.cases.append({"name": name, "status": "NOT_RUN"})
        self.write("cases.json", self.cases)
        self.write(
            "result.json",
            {
                "phase": self.phase,
                "status": status,
                "primary_error": self.primary_error,
                "case_counts": {
                    s: sum(1 for c in self.cases if c["status"] == s)
                    for s in ("PASS", "FAIL", "NOT_RUN")
                },
                "summary": self.summary,
                "security_approval_applied": False,
                "vex_applied": False,
                "production_installation": "NOT_PERFORMED",
            },
        )


# --------------------------------------------------------------------------
# Native helpers
# --------------------------------------------------------------------------


def _bounded(argv: list[str], *, timeout: float, cwd: Path | None) -> Any:
    from fetchnow_release.bounded_subprocess import run_bounded

    return run_bounded(
        argv,
        timeout=timeout,
        cwd=cwd or Path("/"),
        max_stdout_bytes=OUTPUT_LIMIT,
        max_stderr_bytes=OUTPUT_LIMIT,
    )


def run_cmd(
    argv: list[str], *, timeout: float = SHORT_TIMEOUT, cwd: Path | None = None
) -> dict[str, Any]:
    """Run a bounded command; output is redacted because it goes to evidence."""
    from fetchnow_release.redact import redact

    started = time.time()
    result = _bounded(argv, timeout=timeout, cwd=cwd)
    return {
        "argv": [str(a) for a in argv],
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "cancelled": result.cancelled,
        "stdout": redact(result.stdout_text),
        "stderr": redact(result.stderr_text),
        "seconds": round(time.time() - started, 3),
    }


def checked_raw(
    argv: list[str], *, timeout: float = SHORT_TIMEOUT, cwd: Path | None = None
) -> bytes:
    """Unredacted stdout bytes for parsing/hashing; never written verbatim."""
    from fetchnow_release.redact import redact

    result = _bounded(argv, timeout=timeout, cwd=cwd)
    if result.returncode != 0 or result.timed_out or result.cancelled:
        raise AcceptanceError(
            f"command failed: {argv[0]} {argv[1] if len(argv) > 1 else ''} "
            f"rc={result.returncode} timed_out={result.timed_out}: "
            + redact(result.stderr_text)[-2000:]
        )
    return bytes(result.stdout)


def checked(
    argv: list[str], *, timeout: float = SHORT_TIMEOUT, cwd: Path | None = None
) -> str:
    return checked_raw(argv, timeout=timeout, cwd=cwd).decode("utf-8")


def release_cli(args: list[str], *, timeout: float) -> dict[str, Any]:
    """Run the standard ``python -m fetchnow_release`` CLI (no in-process bypass)."""
    return run_cmd(
        [sys.executable, "-m", "fetchnow_release", *args], timeout=timeout, cwd=SCRIPTS
    )


def ok_result(out: dict[str, Any]) -> bool:
    return out["returncode"] == 0 and not out["timed_out"] and not out["cancelled"]


def image_inventory() -> dict[str, Any]:
    return parse_inventory(
        checked(
            ["docker", "image", "ls", "--all", "--no-trunc", "--format", "{{json .}}"]
        )
    )


def inspect_image(ref: str) -> dict[str, Any] | None:
    result = _bounded(
        ["docker", "image", "inspect", "--format", "{{json .}}", ref],
        timeout=SHORT_TIMEOUT,
        cwd=None,
    )
    if result.returncode != 0 or result.timed_out or result.cancelled:
        return None
    value = json.loads(bytes(result.stdout))
    return value if isinstance(value, dict) else None


def image_identity(ref: str) -> dict[str, Any]:
    info = inspect_image(ref)
    if info is None:
        raise AcceptanceError(f"image not present: {ref}")
    config_raw = info.get("Config")
    config: dict[str, Any] = config_raw if isinstance(config_raw, dict) else {}
    labels_raw = config.get("Labels")
    labels: dict[str, Any] = labels_raw if isinstance(labels_raw, dict) else {}
    return {
        "image_id": info.get("Id"),
        "platform": f"{info.get('Os')}/{info.get('Architecture')}",
        "revision_label": labels.get("org.opencontainers.image.revision"),
        "repo_tags": sorted(info.get("RepoTags") or []),
        "repo_digests": sorted(info.get("RepoDigests") or []),
    }


def docker_events(since: float, until: float) -> list[dict[str, Any]]:
    return parse_events(
        checked(
            [
                "docker",
                "events",
                "--since",
                f"{since:.6f}",
                "--until",
                f"{until:.6f}",
                "--format",
                "{{json .}}",
            ]
        )
    )


def build_cache_count() -> int:
    return parse_build_cache_count(
        checked(["docker", "system", "df", "--format", "{{json .}}"])
    )


def containers() -> list[str]:
    return [
        line
        for line in checked(["docker", "ps", "-aq", "--no-trunc"]).splitlines()
        if line.strip()
    ]


def volumes_and_networks() -> dict[str, list[str]]:
    vols = checked(["docker", "volume", "ls", "-q"]).split()
    nets = checked(["docker", "network", "ls", "-q", "--filter", "type=custom"]).split()
    return {"volumes": sorted(vols), "custom_networks": sorted(nets)}


def host_facts() -> dict[str, Any]:
    server = json.loads(checked(["docker", "version", "--format", "{{json .Server}}"]))
    info = json.loads(checked(["docker", "info", "--format", "{{json .}}"]))
    try:
        free: int | None = shutil.disk_usage(str(info.get("DockerRootDir") or "/")).free
    except OSError:
        free = None
    return {
        "uname_machine": platform.machine(),
        "uname_system": platform.system(),
        "docker_server_version": server.get("Version"),
        "docker_api_version": server.get("ApiVersion"),
        "docker_os_arch": f"{info.get('OSType')}/{info.get('Architecture')}",
        "docker_daemon_id": info.get("ID"),
        "docker_storage_driver": info.get("Driver"),
        "docker_root_dir_free_bytes": free,
        "work_fs_free_bytes": shutil.disk_usage(tempfile.gettempdir()).free,
        "runner_name": os.environ.get("RUNNER_NAME"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "github_job": os.environ.get("GITHUB_JOB"),
    }


def api_at_least(version: object, minimum: tuple[int, int]) -> bool:
    try:
        major, minor = (int(x) for x in str(version).split(".")[:2])
    except ValueError:
        return False
    return (major, minor) >= minimum


def git(args: list[str], cwd: Path) -> str:
    return checked(["git", *args], cwd=cwd).strip()


def preconditions(tooling_sha: str) -> dict[str, Any]:
    if not FULL_SHA_RE.fullmatch(tooling_sha):
        raise AcceptanceError("tooling SHA must be a full lowercase SHA")
    head = git(["rev-parse", "HEAD"], ROOT)
    if head != tooling_sha:
        raise AcceptanceError("checkout HEAD differs from tooling SHA")
    if git(["rev-parse", "HEAD^"], ROOT) != TOOLING_PARENT:
        raise AcceptanceError("tooling commit parent differs from reviewed base")
    if git(["status", "--porcelain"], ROOT):
        raise AcceptanceError("tooling checkout is not clean")
    if git(["rev-parse", f"{PRODUCT_SHA}^{{tree}}"], ROOT) != PRODUCT_TREE:
        raise AcceptanceError("product source tree differs from pin")
    if git(["rev-parse", f"{PRODUCT_SHA}^"], ROOT) != PRODUCT_PARENT:
        raise AcceptanceError("product source parent differs from pin")
    ancestor = run_cmd(
        ["git", "merge-base", "--is-ancestor", PRODUCT_SHA, "HEAD"], cwd=ROOT
    )
    if ancestor["returncode"] != 0:
        raise AcceptanceError("product source is not an ancestor of tooling commit")
    facts = host_facts()
    if (
        facts["uname_machine"] not in {"x86_64", "amd64"}
        or facts["uname_system"] != "Linux"
    ):
        raise AcceptanceError("native Linux AMD64 host required")
    if facts["docker_os_arch"] not in {"linux/x86_64", "linux/amd64"}:
        raise AcceptanceError("native linux/amd64 Docker daemon required")
    if not api_at_least(facts["docker_api_version"], (1, 48)):
        raise AcceptanceError("Docker API >= 1.48 required for --platform save/load")
    real_main = run_cmd(
        ["git", "merge-base", "--is-ancestor", PRODUCT_SHA, "refs/remotes/origin/main"],
        cwd=ROOT,
    )
    facts.update(
        {
            "tooling_commit": head,
            "tooling_parent": TOOLING_PARENT,
            "product_revision": PRODUCT_SHA,
            "product_tree": PRODUCT_TREE,
            "product_parent": PRODUCT_PARENT,
            "production_origin_main_contains_product": real_main["returncode"] == 0,
            "release_mode": RELEASE_MODE,
            "compose_overlay": OVERLAY,
        }
    )
    return facts


def fixture_clone(work: Path) -> dict[str, Any]:
    clone = work / "clone"
    checked(
        ["git", "clone", "--quiet", "--local", "--no-hardlinks", str(ROOT), str(clone)],
        timeout=600,
        cwd=work,
    )
    git(["config", "user.email", "saved-image-delivery@example.test"], clone)
    git(["config", "user.name", "Saved Image Delivery Proof"], clone)
    git(["update-ref", "refs/remotes/origin/main", PRODUCT_SHA], clone)
    if git(["rev-parse", "refs/remotes/origin/main"], clone) != PRODUCT_SHA:
        raise AcceptanceError("fixture origin/main was not set")
    if git(["rev-parse", f"{PRODUCT_SHA}^{{tree}}"], clone) != PRODUCT_TREE:
        raise AcceptanceError("fixture clone product tree differs")
    if git(["status", "--porcelain"], clone):
        raise AcceptanceError("fixture clone is not clean")
    return {
        "clone": str(clone),
        "clone_head": git(["rev-parse", "HEAD"], clone),
        "fixture_origin_main": PRODUCT_SHA,
        "fixture_tree": PRODUCT_TREE,
        "authority": FIXTURE_AUTHORITY,
    }


def write_media_env(path: Path, *, project: str, revision: str, deploy: Path) -> None:
    from release_rollout_integration_test import write_env

    port = f"127.0.0.1:{18000 + secrets.randbelow(1800)}"
    write_env(
        path, project=project, revision=revision, gateway_port=port, deploy_root=deploy
    )
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            "MEDIA_EXECUTOR_ENABLED=false\nMEDIA_NET_EXECUTOR_ENABLED=false\n"
            "EGRESS_DENY_CIDRS=172.16.0.0/12\nEGRESS_DENY_IPS=127.0.0.1\n"
        )


def prepare_args(
    *,
    project: str,
    env: Path,
    clone: Path,
    revision: str,
    deploy: Path,
    mode: str = RELEASE_MODE,
    overlay: str = OVERLAY,
    bundle: Path | None = None,
    bundle_sha256: str | None = None,
) -> list[str]:
    args = [
        "prepare",
        "--project-name",
        project,
        "--env-file",
        str(env),
        "--compose-file",
        str(clone / "compose.yaml"),
        "--compose-file",
        str(clone / overlay),
        "--expected-revision",
        revision,
        "--deploy-root",
        str(deploy),
        "--repo-root",
        str(clone),
        "--release-mode",
        mode,
    ]
    if bundle is not None:
        args += [
            "--image-bundle",
            str(bundle),
            "--image-bundle-sha256",
            str(bundle_sha256),
        ]
    return args


def tar_layout(path: Path, limit: int = 400) -> list[dict[str, Any]]:
    import tarfile

    rows: list[dict[str, Any]] = []
    try:
        with tarfile.open(path, mode="r:") as tf:
            for member in tf:
                rows.append(
                    {
                        "name": member.name[:200],
                        "type": member.type.decode(errors="replace"),
                        "size": member.size,
                        "linkname": member.linkname[:200],
                    }
                )
                if len(rows) >= limit:
                    break
    except (OSError, tarfile.TarError) as exc:
        rows.append({"error": str(exc)[:300]})
    return rows


# --------------------------------------------------------------------------
# Builder / exporter
# --------------------------------------------------------------------------

BUILDER_CASES = (
    "builder-preconditions",
    "builder-prepare-build",
    "builder-release-verify",
    "builder-postgres-candidate",
    "builder-export",
    "builder-bundle-validate",
)


def acquire_postgres_candidate(evidence: Evidence) -> dict[str, Any]:
    index_ref = f"{POSTGRES_REPOSITORY}@{POSTGRES_CANDIDATE['registry_index_digest']}"
    manifest_ref = f"{POSTGRES_REPOSITORY}@{POSTGRES_CANDIDATE['platform_manifest_digest_linux_amd64']}"
    config_id = str(POSTGRES_CANDIDATE["config_image_id"])
    preexisting = inspect_image(config_id) is not None

    def raw(ref: str, digest: str) -> tuple[dict[str, Any], str]:
        data = checked_raw(
            ["docker", "buildx", "imagetools", "inspect", "--raw", ref], timeout=300
        )
        exact = hashlib.sha256(data).hexdigest()
        stripped = hashlib.sha256(data.rstrip(b"\n")).hexdigest()
        if exact == digest[7:]:
            match = "exact"
        elif stripped == digest[7:]:
            match = "exact_after_cli_trailing_newline"
        else:
            raise AcceptanceError(f"registry bytes do not hash to {ref}")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise AcceptanceError("registry document is not an object")
        return value, match

    index, index_match = raw(
        index_ref, str(POSTGRES_CANDIDATE["registry_index_digest"])
    )
    amd64 = [
        d.get("digest")
        for d in index.get("manifests", [])
        if isinstance(d, dict)
        and (d.get("platform") or {}).get("os") == "linux"
        and (d.get("platform") or {}).get("architecture") == "amd64"
        and not (d.get("platform") or {}).get("variant")
    ]
    if amd64 != [POSTGRES_CANDIDATE["platform_manifest_digest_linux_amd64"]]:
        raise AcceptanceError("registry index linux/amd64 manifest differs from pin")
    manifest, manifest_match = raw(
        manifest_ref, str(POSTGRES_CANDIDATE["platform_manifest_digest_linux_amd64"])
    )
    if (manifest.get("config") or {}).get("digest") != config_id:
        raise AcceptanceError("platform manifest config digest differs from pin")
    pull = run_cmd(["docker", "pull", "--platform", PLATFORM, index_ref], timeout=900)
    if not ok_result(pull):
        raise AcceptanceError("candidate PostgreSQL pull by digest failed")
    ident = image_identity(config_id)
    if ident["image_id"] != config_id or ident["platform"] != PLATFORM:
        raise AcceptanceError("pulled PostgreSQL config/platform differs from pin")
    if index_ref not in ident["repo_digests"]:
        raise AcceptanceError("pulled PostgreSQL lacks pinned registry index digest")
    if (
        any(t.startswith(f"{POSTGRES_REPOSITORY}:") for t in ident["repo_tags"])
        and not preexisting
    ):
        raise AcceptanceError("digest pull unexpectedly created a postgres tag")
    record = {
        "pull_reference": index_ref,
        "preexisting_config_id_on_builder": preexisting,
        "registry_index_bytes": index_match,
        "platform_manifest_bytes": manifest_match,
        "identity": ident,
        "pinned": POSTGRES_CANDIDATE,
        "live_postgresql": "NOT_CONSULTED (candidate only)",
    }
    evidence.stage("postgres-candidate", record)
    return record


def run_builder(args: argparse.Namespace) -> int:
    evidence = Evidence(args.evidence_dir.resolve(), "builder")
    status = "FAIL"
    try:
        facts = preconditions(args.tooling_sha)
        baseline = image_inventory()
        evidence.stage("host", facts)
        evidence.write("baseline-inventory.json", baseline)
        evidence.summary["host"] = facts
        evidence.case("builder-preconditions", True, containers=containers())

        work = Path(
            tempfile.mkdtemp(prefix="saved-image-builder-", dir=args.work_parent)
        )
        evidence.summary["work_dir"] = str(work)
        fixture = fixture_clone(work)
        evidence.stage("fixture", fixture)
        evidence.summary["fixture"] = fixture
        clone = Path(fixture["clone"])

        from fetchnow_release.rollout_project import make_rollout_project_name

        project = make_rollout_project_name()
        deploy = work / "deploy"
        deploy.mkdir(mode=0o700)
        env = work / "env"
        write_media_env(env, project=project, revision=PRODUCT_SHA, deploy=deploy)
        prep = release_cli(
            prepare_args(
                project=project,
                env=env,
                clone=clone,
                revision=PRODUCT_SHA,
                deploy=deploy,
            ),
            timeout=PREPARE_BUILD_TIMEOUT,
        )
        evidence.stage("prepare-build", prep)
        evidence.case(
            "builder-prepare-build",
            ok_result(prep) and MARK_BUILT in prep["stdout"],
            returncode=prep["returncode"],
        )

        verify = release_cli(
            [
                "verify-release",
                "--expected-revision",
                PRODUCT_SHA,
                "--deploy-root",
                str(deploy),
                "--repo-root",
                str(clone),
            ],
            timeout=SHORT_TIMEOUT,
        )
        from fetchnow_release.deploy_root import release_dir
        from fetchnow_release.manifest import load_manifest, manifest_path
        from fetchnow_release.release_mode import build_services_for_mode

        release = release_dir(deploy, PRODUCT_SHA)
        manifest = load_manifest(manifest_path(release))
        images: dict[str, dict[str, Any]] = {}
        for img in manifest.images:
            ident = image_identity(img.image_id)
            tag_ident = inspect_image(img.reference) or {}
            ident.update(
                {"reference": img.reference, "tag_resolves_to": tag_ident.get("Id")}
            )
            images[img.service] = ident
        release_ok = (
            ok_result(verify)
            and manifest.revision == PRODUCT_SHA
            and manifest.tree_oid == PRODUCT_TREE
            and manifest.release_mode == RELEASE_MODE
            and manifest.compose_overlay == OVERLAY
            and set(images) == set(build_services_for_mode(RELEASE_MODE))
            and all(
                i["platform"] == PLATFORM
                and i["revision_label"] == PRODUCT_SHA
                and i["tag_resolves_to"] == i["image_id"]
                for i in images.values()
            )
        )
        evidence.write("builder-release.json", manifest.to_dict())
        evidence.stage("release-verify", {"verify": verify, "images": images})
        evidence.case(
            "builder-release-verify",
            release_ok,
            tree_oid=manifest.tree_oid,
            contract_hashes=manifest.contract_hashes,
            source_contract_version=manifest.source_contract_version,
        )

        pg = acquire_postgres_candidate(evidence)
        images["postgres"] = dict(pg["identity"], reference=None)
        evidence.case(
            "builder-postgres-candidate", True, image_id=pg["identity"]["image_id"]
        )

        pg_json = work / "postgres-identity.json"
        pg_json.write_text(
            json.dumps(POSTGRES_CANDIDATE, indent=2) + "\n", encoding="utf-8"
        )
        bundle_dir = args.bundle_dir
        if bundle_dir.exists() or bundle_dir.is_symlink():
            raise AcceptanceError("bundle output must not pre-exist")
        export = release_cli(
            [
                "export-images",
                "--expected-revision",
                PRODUCT_SHA,
                "--deploy-root",
                str(deploy),
                "--repo-root",
                str(clone),
                "--output",
                str(bundle_dir),
                "--postgres-identity-json",
                str(pg_json),
            ],
            timeout=EXPORT_TIMEOUT,
        )
        evidence.stage("export", export)
        if not ok_result(export):
            layouts = {
                p.name: tar_layout(p)
                for p in sorted(bundle_dir.glob("*.tar"))
                if bundle_dir.is_dir()
            }
            evidence.write("export-failure-tar-layout.json", layouts)
        evidence.case(
            "builder-export", ok_result(export), returncode=export["returncode"]
        )

        from fetchnow_release.image_bundle import BUNDLE_FILE, validate_bundle

        bundle_sha = sha256_file(bundle_dir / BUNDLE_FILE)
        printed = re.findall(r"bundle_sha256=([0-9a-f]{64})", export["stdout"])
        validated = validate_bundle(bundle_dir, expected_sha256=bundle_sha)
        rows = {row["service"]: row for row in validated.images}
        files = {
            p.name: {"sha256": sha256_file(p), "size_bytes": p.stat().st_size}
            for p in sorted(bundle_dir.iterdir())
        }
        verify_after = release_cli(
            [
                "verify-release",
                "--expected-revision",
                PRODUCT_SHA,
                "--deploy-root",
                str(deploy),
                "--repo-root",
                str(clone),
            ],
            timeout=SHORT_TIMEOUT,
        )
        bundle_ok = (
            printed == [bundle_sha]
            and set(rows) == set(images)
            and all(rows[s]["image_id"] == images[s]["image_id"] for s in rows)
            and all(
                files[rows[s]["archive"]]["sha256"] == rows[s]["sha256"] for s in rows
            )
            and ok_result(verify_after)
        )
        manifest_sha = sha256_file(bundle_dir / "release.json")
        evidence.summary.update(
            {
                "tooling_commit": facts["tooling_commit"],
                "product_revision": PRODUCT_SHA,
                "product_tree": PRODUCT_TREE,
                "fixture_origin_main": PRODUCT_SHA,
                "fixture_authority": FIXTURE_AUTHORITY,
                "release_mode": RELEASE_MODE,
                "compose_overlay": OVERLAY,
                "bundle_sha256": bundle_sha,
                "release_manifest_sha256": manifest_sha,
                "release_tree_oid": manifest.tree_oid,
                "release_contract_hashes": manifest.contract_hashes,
                "images": images,
                "archives": {
                    s: {k: rows[s][k] for k in ("archive", "sha256", "size_bytes")}
                    for s in rows
                },
                "bundle_files": files,
                "postgres": {k: v for k, v in POSTGRES_CANDIDATE.items()},
                "docker_daemon_id": facts["docker_daemon_id"],
            }
        )
        evidence.case("builder-bundle-validate", bundle_ok, bundle_sha256=bundle_sha)
        (evidence.dir / "bundle-sha256.txt").write_text(
            bundle_sha + "\n", encoding="utf-8"
        )
        hits = privacy_hits(evidence.dir, ("A" * 32,))
        if hits:
            raise AcceptanceError(f"privacy check failed: {hits}")
        status = "PASS"
    except Exception as exc:  # noqa: BLE001 — primary error recorded, never hidden
        evidence.primary_error = f"{type(exc).__name__}: {exc}"
        evidence.write(
            "primary-error.json",
            {
                "error": evidence.primary_error,
                "traceback": traceback.format_exc()[-6000:],
            },
        )
    finally:
        evidence.finish(status, BUILDER_CASES)
    return 0 if status == "PASS" else 1


# --------------------------------------------------------------------------
# Importer
# --------------------------------------------------------------------------

IMPORTER_CASES = (
    "importer-preconditions",
    "importer-candidates-absent",
    "importer-received-bundle",
    "neg-wrong-bundle-checksum",
    "neg-corrupted-tar",
    "neg-missing-file",
    "neg-extra-file",
    "neg-revision-mismatch",
    "neg-mode-mismatch",
    "neg-overlay-mismatch",
    "import-positive",
    "import-identity",
    "import-idempotent",
    "release-verify-health-identity",
    "neg-conflicting-revision-tag",
    "neg-receipt-missing",
    "neg-receipt-mismatch",
    "postgres-and-live-state-unchanged",
    "received-bundle-unchanged",
)


class Importer:
    def __init__(self, args: argparse.Namespace, evidence: Evidence) -> None:
        self.args = args
        self.ev = evidence
        self.bundle: Path = args.bundle.resolve()
        self.sha: str = args.bundle_sha256
        self.work = Path(
            tempfile.mkdtemp(prefix="saved-image-importer-", dir=args.work_parent)
        )
        self.clone = Path()
        self.project = ""
        self.deploy = Path()
        self.env = Path()
        self.builder: dict[str, Any] = {}
        self.baseline: dict[str, Any] = {}

    def case_env(self, name: str, revision: str = PRODUCT_SHA) -> tuple[Path, Path]:
        case_dir = self.work / "cases" / name
        case_dir.mkdir(mode=0o700, parents=True)
        deploy = case_dir / "deploy"
        deploy.mkdir(mode=0o700)
        env = case_dir / "env"
        write_media_env(env, project=self.project, revision=revision, deploy=deploy)
        return deploy, env

    def negative(
        self,
        name: str,
        *,
        expected: tuple[str, ...],
        bundle: Path,
        sha: str | None = None,
        revision: str = PRODUCT_SHA,
        mode: str = RELEASE_MODE,
        overlay: str = OVERLAY,
        deploy: Path | None = None,
        env: Path | None = None,
    ) -> None:
        if deploy is None or env is None:
            deploy, env = self.case_env(name, revision)
        before_inventory = image_inventory()
        before_tree = directory_digest(deploy)
        before_state = published_release_state(deploy, PRODUCT_SHA)
        t0 = time.time()
        out = release_cli(
            prepare_args(
                project=self.project,
                env=env,
                clone=self.clone,
                revision=revision,
                deploy=deploy,
                mode=mode,
                overlay=overlay,
                bundle=bundle,
                bundle_sha256=sha or self.sha,
            ),
            timeout=IMPORT_TIMEOUT,
        )
        t1 = time.time()
        events = docker_events(t0, t1)
        after_inventory = image_inventory()
        after_state = published_release_state(deploy, PRODUCT_SHA)
        text = out["stdout"] + out["stderr"]
        checks = {
            "refused": out["returncode"] not in (0, None) and not out["timed_out"],
            "expected_reason": any(e in text for e in expected),
            "no_import_message": MARK_IMPORTED not in text,
            "no_docker_mutation_events": not event_violations(
                events, allowed_image_actions=frozenset()
            ),
            "inventory_unchanged": before_inventory == after_inventory,
            "no_new_release_published": (
                after_state["release_published"] == before_state["release_published"]
                and not after_state["incomplete_entries"]
            ),
            "no_current_or_media_authority": not after_state["current_present"]
            and not after_state["media_authority_present"],
        }
        if before_state["release_published"]:
            checks["existing_release_unchanged"] = (
                directory_digest(deploy) == before_tree
            )
        self.ev.case(
            name,
            all(checks.values()),
            checks=checks,
            returncode=out["returncode"],
            message=text.strip()[-600:],
            events=events,
        )

    def run(self) -> None:
        ev = self.ev
        facts = preconditions(self.args.tooling_sha)
        ev.stage("host", facts)
        ev.summary["host"] = facts
        if not SHA256_RE.fullmatch(self.sha):
            raise AcceptanceError(
                "expected bundle SHA-256 from builder output is malformed"
            )
        self.builder = json.loads(self.args.builder_summary.read_text(encoding="utf-8"))
        builder_summary = self.builder.get("summary") or {}
        identity_checks = {
            "builder_status_pass": self.builder.get("status") == "PASS",
            "same_tooling_commit": builder_summary.get("tooling_commit")
            == facts["tooling_commit"],
            "same_product": builder_summary.get("product_revision") == PRODUCT_SHA
            and builder_summary.get("product_tree") == PRODUCT_TREE,
            "workflow_output_matches_builder_record": builder_summary.get(
                "bundle_sha256"
            )
            == self.sha,
            "different_docker_daemon": bool(facts["docker_daemon_id"])
            and builder_summary.get("docker_daemon_id") != facts["docker_daemon_id"],
            "no_containers": not containers(),
        }
        ev.case(
            "importer-preconditions",
            all(identity_checks.values()),
            checks=identity_checks,
        )
        builder_images: dict[str, dict[str, Any]] = builder_summary["images"]
        expected_ids = {svc: row["image_id"] for svc, row in builder_images.items()}

        self.baseline = image_inventory()
        ev.write("baseline-inventory.json", self.baseline)
        present = {
            svc: image_id
            for svc, image_id in expected_ids.items()
            if image_id in self.baseline["ids"] or inspect_image(image_id) is not None
        }
        ev.case(
            "importer-candidates-absent",
            len(expected_ids) == 7 and not present,
            expected_ids=expected_ids,
            present_before_import=present,
        )

        received = bundle_file_hashes(self.bundle)
        builder_files = {
            k: v["sha256"] for k, v in builder_summary["bundle_files"].items()
        }
        ev.case(
            "importer-received-bundle",
            received == builder_files and received.get("image-bundle.json") == self.sha,
            received=received,
        )

        fixture = fixture_clone(self.work)
        ev.stage("fixture", fixture)
        ev.summary["fixture"] = fixture
        self.clone = Path(fixture["clone"])
        from fetchnow_release.rollout_project import make_rollout_project_name

        self.project = make_rollout_project_name()
        cache_before = build_cache_count()
        live_before = volumes_and_networks()

        # Refusals before any Docker mutation: candidates still absent here.
        cases = self.work / "bundles"
        wrong = self.sha[:-1] + ("0" if self.sha[-1] != "0" else "1")
        b = cases / "wrong-sha"
        link_copy(self.bundle, b)
        self.negative(
            "neg-wrong-bundle-checksum",
            expected=(REASON_CHECKSUM,),
            bundle=b,
            sha=wrong,
        )
        b = cases / "corrupt"
        link_copy(self.bundle, b, real_copy=("api.tar",))
        flip_byte(b / "api.tar", (b / "api.tar").stat().st_size // 2)
        self.negative("neg-corrupted-tar", expected=(REASON_ARCHIVE + "api",), bundle=b)
        b = cases / "missing"
        link_copy(self.bundle, b, omit=("web.tar",))
        self.negative(
            "neg-missing-file", expected=(OS_MISSING_FILE, REASON_FILES), bundle=b
        )
        b = cases / "extra"
        link_copy(self.bundle, b)
        (b / "extra.txt").write_text("unexpected\n", encoding="utf-8")
        self.negative("neg-extra-file", expected=(REASON_FILES,), bundle=b)
        b = cases / "revision"
        link_copy(self.bundle, b)
        self.negative(
            "neg-revision-mismatch",
            expected=(REASON_REVISION_MODE,),
            bundle=b,
            revision=PRODUCT_PARENT,
        )
        b = cases / "mode"
        link_copy(self.bundle, b)
        self.negative(
            "neg-mode-mismatch",
            expected=(REASON_REVISION_MODE,),
            bundle=b,
            mode="canonical",
        )
        b = cases / "overlay"
        link_copy(self.bundle, b)
        self.negative(
            "neg-overlay-mismatch",
            expected=(REASON_OVERLAY,),
            bundle=b,
            overlay="compose.production.yaml",
        )
        after_negatives = image_inventory()
        if any(i in after_negatives["ids"] for i in expected_ids.values()):
            raise AcceptanceError("candidate image appeared during refusal cases")

        # Positive import on the main deploy root.
        self.deploy = self.work / "deploy"
        self.deploy.mkdir(mode=0o700)
        self.env = self.work / "env"
        write_media_env(
            self.env, project=self.project, revision=PRODUCT_SHA, deploy=self.deploy
        )
        env_sha_before = sha256_file(self.env)
        containers_before = containers()
        state_before = published_release_state(self.deploy, PRODUCT_SHA)
        inv_before = image_inventory()
        t0 = time.time()
        out = release_cli(
            prepare_args(
                project=self.project,
                env=self.env,
                clone=self.clone,
                revision=PRODUCT_SHA,
                deploy=self.deploy,
                bundle=self.bundle,
                bundle_sha256=self.sha,
            ),
            timeout=IMPORT_TIMEOUT,
        )
        t1 = time.time()
        events = docker_events(t0, t1)
        inv_after = image_inventory()
        delta = inventory_delta(inv_before, inv_after)
        state_after = published_release_state(self.deploy, PRODUCT_SHA)
        cache_after = build_cache_count()
        ev.stage("import-positive", {"prepare": out, "events": events, "delta": delta})
        from fetchnow_release.manifest import load_manifest

        app_refs = {
            img.reference: img.image_id
            for img in load_manifest(self.bundle / "release.json").images
        }
        loaded_ids = {
            e["actor_id"]
            for e in events
            if e["type"] == "image" and e["action"] == "load"
        }
        checks = {
            "prepare_ok": ok_result(out),
            "import_message": MARK_IMPORTED in out["stdout"],
            "atomic_publish_message": MARK_PUBLISHED in out["stdout"],
            "no_build_message": MARK_BUILT not in out["stdout"],
            "only_load_and_tag_events": not event_violations(
                events, allowed_image_actions=IMPORT_IMAGE_ACTIONS
            ),
            "no_pull_events": not any(e["action"] == "pull" for e in events),
            "load_events_present": any(e["action"] == "load" for e in events),
            "new_ids_exactly_builder_ids": sorted(delta["new_ids"])
            == sorted(expected_ids.values()),
            "new_tags_exactly_app_refs": delta["new_tags"]
            == dict(sorted(app_refs.items())),
            "no_removed_or_retargeted": not delta["removed_ids"]
            and not delta["removed_tags"]
            and not delta["retargeted_tags"],
            "build_cache_unchanged": cache_before == cache_after,
            "release_published": state_after["release_published"]
            and not state_before["release_published"],
            "no_incomplete_left": not state_after["incomplete_entries"],
            "current_unchanged_absent": not state_before["current_present"]
            and not state_after["current_present"],
            "media_authority_unchanged_absent": not state_before[
                "media_authority_present"
            ]
            and not state_after["media_authority_present"],
            "activation_flags_env_unchanged": sha256_file(self.env) == env_sha_before,
            "containers_unchanged": containers() == containers_before,
        }
        ev.case(
            "import-positive",
            all(checks.values()),
            checks=checks,
            loaded_event_actor_ids=sorted(loaded_ids),
            build_cache=[cache_before, cache_after],
        )

        self.identity_checks(builder_summary, expected_ids)
        self.idempotent()
        self.health_identity(expected_ids)
        self.tag_conflict(app_refs)
        self.receipt_cases()

        live_after = volumes_and_networks()
        final_inventory = image_inventory()
        pg_id = str(POSTGRES_CANDIDATE["config_image_id"])
        pg_ident = image_identity(pg_id)
        postgres_tags = [
            t
            for t in final_inventory["tags"]
            if t.startswith(f"{POSTGRES_REPOSITORY}:")
        ]
        baseline_pg_tags = [
            t for t in self.baseline["tags"] if t.startswith(f"{POSTGRES_REPOSITORY}:")
        ]
        checks = {
            "candidate_loaded_untagged": pg_ident["image_id"] == pg_id
            and not pg_ident["repo_tags"],
            "postgres_tags_unchanged": postgres_tags == baseline_pg_tags,
            "no_containers_started": not containers(),
            "volumes_networks_unchanged": live_before == live_after,
            "current_absent": not published_release_state(self.deploy, PRODUCT_SHA)[
                "current_present"
            ],
            "media_authority_absent": not published_release_state(
                self.deploy, PRODUCT_SHA
            )["media_authority_present"],
        }
        ev.case(
            "postgres-and-live-state-unchanged",
            all(checks.values()),
            checks=checks,
            postgres_identity=pg_ident,
            postgres_tags=postgres_tags,
        )
        final_hashes = bundle_file_hashes(self.bundle)
        ev.case(
            "received-bundle-unchanged", final_hashes == received, hashes=final_hashes
        )

    def identity_checks(
        self, builder_summary: dict[str, Any], expected_ids: dict[str, str]
    ) -> None:
        from fetchnow_release.deploy_root import release_dir
        from fetchnow_release.image_bundle import IMPORT_RECEIPT
        from fetchnow_release.manifest import load_manifest, manifest_path

        release = release_dir(self.deploy, PRODUCT_SHA)
        prepared = load_manifest(manifest_path(release))
        bundled = load_manifest(self.bundle / "release.json")
        importer_images: dict[str, dict[str, Any]] = {}
        for svc, image_id in expected_ids.items():
            importer_images[svc] = image_identity(image_id)
        receipt = json.loads((release / IMPORT_RECEIPT).read_text(encoding="utf-8"))
        archives = {
            svc: sha256_file(self.bundle / row["archive"])
            for svc, row in builder_summary["archives"].items()
        }
        mismatches = compare_builder_importer(
            builder_summary["images"], importer_images
        )
        checks = {
            "importer_ids_platforms_labels_equal_builder": not mismatches,
            "app_labels_equal_product": all(
                importer_images[s]["revision_label"] == PRODUCT_SHA
                for s in expected_ids
                if s != "postgres"
            ),
            "all_linux_amd64": all(
                i["platform"] == PLATFORM for i in importer_images.values()
            ),
            "archive_sha256_equal_builder": all(
                archives[s] == builder_summary["archives"][s]["sha256"]
                for s in archives
            ),
            "prepared_manifest_equals_bundle": prepared.to_dict() == bundled.to_dict(),
            "release_manifest_sha256_equal_builder": sha256_file(
                self.bundle / "release.json"
            )
            == builder_summary["release_manifest_sha256"],
            "tree_equal_builder_and_product": prepared.tree_oid
            == builder_summary["release_tree_oid"]
            == PRODUCT_TREE,
            "contract_hashes_equal_builder": prepared.contract_hashes
            == builder_summary["release_contract_hashes"],
            "receipt_bundle_sha256": receipt.get("bundle_sha256") == self.sha,
            "receipt_image_ids_equal_builder": receipt.get("image_ids") == expected_ids,
            "receipt_postgres_candidate_only": receipt.get("postgres_candidate_only")
            is True,
            "receipt_postgres_registry_identity": receipt.get(
                "postgres_registry_identity"
            )
            == {
                "registry_index_digest": POSTGRES_CANDIDATE["registry_index_digest"],
                "platform_manifest_digest_linux_amd64": POSTGRES_CANDIDATE[
                    "platform_manifest_digest_linux_amd64"
                ],
            },
            "receipt_no_security_approval": receipt.get("security_approval_applied")
            is False,
        }
        self.ev.summary.update(
            {
                "importer_images": importer_images,
                "receipt": receipt,
                "archives_sha256": archives,
            }
        )
        self.ev.case(
            "import-identity",
            all(checks.values()),
            checks=checks,
            mismatches=mismatches,
        )

    def idempotent(self) -> None:
        from fetchnow_release.deploy_root import release_dir

        release = release_dir(self.deploy, PRODUCT_SHA)
        tree_before = directory_digest(release)
        inv_before = image_inventory()
        t0 = time.time()
        out = release_cli(
            prepare_args(
                project=self.project,
                env=self.env,
                clone=self.clone,
                revision=PRODUCT_SHA,
                deploy=self.deploy,
                bundle=self.bundle,
                bundle_sha256=self.sha,
            ),
            timeout=IMPORT_TIMEOUT,
        )
        t1 = time.time()
        events = docker_events(t0, t1)
        checks = {
            "prepare_ok": ok_result(out),
            "already_prepared": MARK_IDEMPOTENT in out["stdout"],
            "no_docker_events": not events,
            "inventory_unchanged": image_inventory() == inv_before,
            "release_unchanged": directory_digest(release) == tree_before,
        }
        self.ev.case(
            "import-idempotent",
            all(checks.values()),
            checks=checks,
            message=out["stdout"][-400:],
        )

    def health_identity(self, expected_ids: dict[str, str]) -> None:
        from fetchnow_release.deploy_root import release_dir
        from fetchnow_release.image_identity import assert_release_images_present
        from fetchnow_release.manifest import load_manifest, manifest_path
        from fetchnow_release.override import image_ids_from_manifest

        verify = release_cli(
            [
                "verify-release",
                "--expected-revision",
                PRODUCT_SHA,
                "--deploy-root",
                str(self.deploy),
                "--repo-root",
                str(self.clone),
            ],
            timeout=SHORT_TIMEOUT,
        )
        manifest = load_manifest(manifest_path(release_dir(self.deploy, PRODUCT_SHA)))
        from_manifest = image_ids_from_manifest(manifest)
        inspected = assert_release_images_present(manifest)
        media = {img.service: img.image_id for img in manifest.images}
        checks = {
            "verify_release_cli_ok": ok_result(verify),
            "health_identity_map_matches_inspect": inspected == from_manifest,
            "health_identity_equals_builder": from_manifest["api"]
            == expected_ids["api"]
            and from_manifest["worker"] == expected_ids["api"]
            and from_manifest["web"] == expected_ids["web"]
            and from_manifest["gateway"] == expected_ids["gateway"],
            "media_ids_equal_builder": all(media[s] == expected_ids[s] for s in media),
        }
        self.ev.case(
            "release-verify-health-identity",
            all(checks.values()),
            checks=checks,
            scope="verify-release + health/rollout image-identity primitives; no containers started; not the full native matrix",
        )

    def tag_conflict(self, app_refs: dict[str, str]) -> None:
        web_ref = next(r for r in app_refs if r.startswith("fetchnow-web:"))
        api_ref = next(r for r in app_refs if r.startswith("fetchnow-api:"))
        web_id, api_id = app_refs[web_ref], app_refs[api_ref]
        inventory_before = image_inventory()
        b = self.work / "bundles" / "tag-conflict"
        link_copy(self.bundle, b)
        restored = False
        try:
            checked(["docker", "image", "tag", api_id, web_ref])
            self.negative(
                "neg-conflicting-revision-tag",
                expected=(REASON_TAG,),
                bundle=b,
            )
        finally:
            checked(["docker", "image", "tag", web_id, web_ref])
            restored = (inspect_image(web_ref) or {}).get("Id") == web_id
            self.ev.stage(
                "tag-conflict-restore",
                {
                    "reference": web_ref,
                    "restored": restored,
                    "inventory_equal": image_inventory() == inventory_before,
                },
            )
        if not restored or image_inventory() != inventory_before:
            raise AcceptanceError("disposable tag was not restored exactly")

    def receipt_cases(self) -> None:
        from fetchnow_release.image_bundle import IMPORT_RECEIPT

        for name, mutate in (
            ("neg-receipt-missing", "delete"),
            ("neg-receipt-mismatch", "mismatch"),
        ):
            case_dir = self.work / "cases" / name
            case_dir.mkdir(mode=0o700, parents=True)
            deploy = case_dir / "deploy"
            shutil.copytree(self.deploy, deploy, symlinks=True)
            receipt = deploy / "releases" / PRODUCT_SHA / IMPORT_RECEIPT
            if mutate == "delete":
                receipt.unlink()
            else:
                data = json.loads(receipt.read_text(encoding="utf-8"))
                data["bundle_sha256"] = "0" * 64
                receipt.write_text(
                    json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
            env = case_dir / "env"
            write_media_env(
                env, project=self.project, revision=PRODUCT_SHA, deploy=deploy
            )
            b = self.work / "bundles" / name
            link_copy(self.bundle, b)
            self.negative(
                name,
                expected=(REASON_RECEIPT, OS_MISSING_FILE),
                bundle=b,
                deploy=deploy,
                env=env,
            )


def run_importer(args: argparse.Namespace) -> int:
    evidence = Evidence(args.evidence_dir.resolve(), "importer")
    status = "FAIL"
    importer: Importer | None = None
    try:
        importer = Importer(args, evidence)
        evidence.summary["work_dir"] = str(importer.work)
        importer.run()
        hits = privacy_hits(evidence.dir, ("A" * 32,))
        if hits:
            raise AcceptanceError(f"privacy check failed: {hits}")
        status = "PASS"
    except Exception as exc:  # noqa: BLE001
        evidence.primary_error = f"{type(exc).__name__}: {exc}"
        evidence.write(
            "primary-error.json",
            {
                "error": evidence.primary_error,
                "traceback": traceback.format_exc()[-6000:],
            },
        )
    finally:
        evidence.finish(status, IMPORTER_CASES)
    return 0 if status == "PASS" else 1


# --------------------------------------------------------------------------
# Inventory + scoped cleanup (only resources new since this job's baseline)
# --------------------------------------------------------------------------


def make_writable(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                path.chmod(
                    stat.S_IMODE(path.stat().st_mode)
                    | stat.S_IWUSR
                    | (stat.S_IXUSR if path.is_dir() else 0)
                )
    if root.exists() and not root.is_symlink():
        root.chmod(stat.S_IMODE(root.stat().st_mode) | stat.S_IWUSR | stat.S_IXUSR)


def run_cleanup(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {"steps": [], "errors": []}
    try:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        current = image_inventory()
        delta = inventory_delta(baseline, current)
        report["delta_before_cleanup"] = delta
        running = containers()
        report["containers"] = running
        if running:
            report["errors"].append("containers present; image removal refused")
        else:
            for ref in delta["new_tags"]:
                out = run_cmd(["docker", "image", "rm", ref], timeout=300)
                report["steps"].append({"remove_tag": ref, "rc": out["returncode"]})
            remaining = image_inventory()
            for image_id in sorted(set(remaining["ids"]) - set(baseline["ids"])):
                out = run_cmd(["docker", "image", "rm", image_id], timeout=300)
                report["steps"].append({"remove_id": image_id, "rc": out["returncode"]})
        final = image_inventory()
        final_delta = inventory_delta(baseline, final)
        report["delta_after_cleanup"] = final_delta
        if final_delta["new_ids"] or final_delta["new_tags"]:
            report["errors"].append("run-created images remain")
        if (
            final_delta["removed_ids"]
            or final_delta["removed_tags"]
            or final_delta["retargeted_tags"]
        ):
            report["errors"].append("pre-existing image/tag changed")
        for raw in args.remove_dir:
            path = Path(raw)
            if path.exists():
                make_writable(path)
                shutil.rmtree(path)
            report["steps"].append(
                {"remove_dir": str(path), "absent": not path.exists()}
            )
            if path.exists():
                report["errors"].append(f"directory remains: {path}")
        report["build_cache_note"] = (
            "build cache left on disposable runner (no global prune)"
        )
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    report["cleanup_ok"] = not report["errors"]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"cleanup_ok": report["cleanup_ok"], "errors": report["errors"]}))
    return 0 if report["cleanup_ok"] else 1


def run_inventory(args: argparse.Namespace) -> int:
    if args.out.exists():
        print("refusing to overwrite inventory", file=sys.stderr)
        return 1
    args.out.write_text(
        json.dumps(image_inventory(), indent=2) + "\n", encoding="utf-8"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("builder")
    b.add_argument("--tooling-sha", required=True)
    b.add_argument("--evidence-dir", type=Path, required=True)
    b.add_argument("--bundle-dir", type=Path, required=True)
    b.add_argument("--work-parent", type=Path, required=True)
    i = sub.add_parser("importer")
    i.add_argument("--tooling-sha", required=True)
    i.add_argument("--evidence-dir", type=Path, required=True)
    i.add_argument("--bundle", type=Path, required=True)
    i.add_argument("--bundle-sha256", required=True)
    i.add_argument("--builder-summary", type=Path, required=True)
    i.add_argument("--work-parent", type=Path, required=True)
    inv = sub.add_parser("inventory")
    inv.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("cleanup")
    c.add_argument("--baseline", type=Path, required=True)
    c.add_argument("--report", type=Path, required=True)
    c.add_argument("--remove-dir", action="append", default=[])
    args = parser.parse_args(argv)
    if args.command in {"builder", "importer"}:
        if os.environ.get(EXECUTE_ENV) != "1":
            print(f"NOT_RUN: set {EXECUTE_ENV}=1 for explicit native execution")
            return 2
        if platform.system() != "Linux" or platform.machine() not in {
            "x86_64",
            "amd64",
        }:
            print("NOT_RUN: requires native Linux AMD64")
            return 2
        return run_builder(args) if args.command == "builder" else run_importer(args)
    if args.command == "inventory":
        return run_inventory(args)
    return run_cleanup(args)


if __name__ == "__main__":
    raise SystemExit(main())
