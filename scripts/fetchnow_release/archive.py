"""Exact-revision git archive materialization with safe extraction."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .c2_constants import (
    CONTRACT_HASH_FILES,
    CURRENT_PREPARE_SOURCE_CONTRACT_VERSION,
    FORBIDDEN_SOURCE_PATHS,
    LFS_POINTER_PREFIX,
    SOURCE_DIRNAME,
)
from .revision import validate_full_sha
from .source_contract import required_source_files_for_version

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_GITLINK_MODE = 0o160000
_MODE_TYPE_MASK = 0o170000


class ArchiveError(ValueError):
    """Source archive / extraction failure."""


@dataclass(frozen=True)
class MaterializedSource:
    revision: str
    tree_oid: str
    source_dir: Path
    archive_path: Path
    contract_hashes: dict[str, str]


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )


def resolve_tree_oid(repo: Path, revision: str) -> str:
    rev = validate_full_sha(revision)
    proc = _run_git(repo, "rev-parse", "--verify", f"{rev}^{{tree}}")
    if proc.returncode != 0:
        raise ArchiveError(f"could not resolve tree OID for {rev}")
    oid = proc.stdout.strip().lower()
    if not _SHA_RE.fullmatch(oid):
        raise ArchiveError(f"unexpected tree OID: {oid!r}")
    return oid


def stream_git_archive(repo: Path, revision: str, archive_path: Path) -> None:
    rev = validate_full_sha(revision)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with open(archive_path, "wb") as fh:
        proc = subprocess.Popen(
            ["git", "archive", "--format=tar", rev],
            cwd=str(repo),
            stdout=fh,
            stderr=subprocess.PIPE,
        )
        _, err = proc.communicate()
        if proc.returncode != 0:
            raise ArchiveError(
                "git archive failed: "
                + (err.decode("utf-8", errors="replace").strip() or "unknown error")
            )
        fh.flush()
        os.fsync(fh.fileno())
    if not archive_path.is_file() or archive_path.stat().st_size == 0:
        raise ArchiveError("git archive produced empty or missing file")


def verify_archive_commit(archive_path: Path, expected_revision: str) -> str:
    """Verify Git archive pax/comment commit ID equals expected full SHA."""
    expected = validate_full_sha(expected_revision)
    found: str | None = None
    with tarfile.open(archive_path, mode="r:") as tf:
        for member in tf:
            comment = (member.pax_headers or {}).get("comment")
            if comment and _SHA_RE.fullmatch(comment.strip().lower()):
                found = comment.strip().lower()
                break
    if found is None:
        # Fallback: read raw bytes for pax 'comment=<sha>' (ustar extended).
        raw = archive_path.read_bytes()
        marker = b"comment="
        idx = raw.find(marker)
        if idx >= 0:
            frag = raw[idx + len(marker) : idx + len(marker) + 64]
            m = re.search(rb"([0-9a-f]{40})", frag)
            if m:
                found = m.group(1).decode("ascii")
    if found is None:
        raise ArchiveError(
            "git archive did not embed a verifiable commit ID in pax headers"
        )
    if found != expected:
        raise ArchiveError(
            f"archive commit {found} does not match expected revision {expected}"
        )
    return found


def _is_within_directory(directory: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def _reject_unsafe_name(name: str, *, kind: str) -> None:
    if not name or name.endswith("\x00"):
        raise ArchiveError(f"empty or NUL {kind} rejected: {name!r}")
    if _CONTROL_RE.search(name):
        raise ArchiveError(f"control characters in {kind} rejected: {name!r}")
    if name.startswith("/") or name.startswith("\\"):
        raise ArchiveError(f"absolute archive path rejected: {name!r}")
    # Windows drive / UNC style escapes
    if re.match(r"^[A-Za-z]:", name) or name.startswith("//") or name.startswith("\\\\"):
        raise ArchiveError(f"drive/UNC archive path rejected: {name!r}")
    parts = Path(name).parts
    if ".." in parts:
        raise ArchiveError(f"path traversal rejected: {name!r}")


def _is_special_node(member: tarfile.TarInfo) -> bool:
    if member.isdev() or member.isfifo():
        return True
    socktype = getattr(tarfile, "SOCKTYPE", b"S")
    return member.type == socktype


def _safe_extract_filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo:
    """Python 3.12+ extract filter: reject unsafe members, strip setuid/setgid."""
    _reject_unsafe_name(member.name, kind="member path")
    if _is_special_node(member):
        raise ArchiveError(f"device/FIFO/socket rejected: {member.name!r}")
    if (member.mode & _MODE_TYPE_MASK) == _GITLINK_MODE:
        raise ArchiveError(f"gitlink/submodule mode rejected: {member.name!r}")
    if member.issym() or member.islnk():
        link = member.linkname or ""
        _reject_unsafe_name(link, kind="link target")
        if link.startswith("/") or ".." in Path(link).parts:
            raise ArchiveError(f"unsafe link rejected: {member.name!r} -> {link!r}")
        # Hardlink/symlink target must stay inside destination once joined.
        dest = Path(dest_path)
        link_parent = (dest / member.name).parent
        resolved_link = (link_parent / link).resolve()
        try:
            resolved_link.relative_to(dest.resolve())
        except ValueError as exc:
            raise ArchiveError(
                f"link escape rejected: {member.name!r} -> {link!r}"
            ) from exc
    # Strip setuid/setgid/sticky; preserve user rwx bits for files/dirs.
    mode = member.mode & 0o777
    mode &= ~(stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
    member.mode = mode
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    return member


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "dir"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    if member.isreg() or member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, b"\0", b"0"):
        return "file"
    return "other"


def extract_archive(archive_path: Path, destination: Path) -> None:
    if destination.exists():
        raise ArchiveError(f"extraction destination already exists: {destination}")
    destination.mkdir(parents=True, mode=0o750)
    seen: dict[str, str] = {}
    with tarfile.open(archive_path, mode="r:") as tf:
        members = tf.getmembers()
        abs_dest = destination.resolve()
        # Complete pre-scan before any extractall write.
        for member in members:
            norm = str(Path(member.name))
            kind = _member_kind(member)
            if norm in seen:
                raise ArchiveError(f"duplicate archive path: {norm!r}")
            # File/directory type collision on the same path is a duplicate above;
            # also reject a file whose parent path is already a non-dir member.
            parent = str(Path(norm).parent)
            if parent not in ("", ".") and parent in seen and seen[parent] != "dir":
                raise ArchiveError(
                    f"type collision: parent {parent!r} is {seen[parent]}, "
                    f"cannot extract {norm!r}"
                )
            seen[norm] = kind
            _safe_extract_filter(member, str(destination))
            # Lexical containment without following not-yet-extracted links.
            abs_target = (destination / member.name).resolve(strict=False)
            try:
                abs_target.relative_to(abs_dest)
            except ValueError as exc:
                raise ArchiveError(
                    f"extraction escape rejected: {member.name!r}"
                ) from exc
        tf.extractall(  # noqa: S202 — members validated + filter applied
            destination,
            members=members,
            filter=_safe_extract_filter,
        )


def detect_lfs_pointers(source_dir: Path, *, source_contract_version: int) -> None:
    """Fail closed if required build inputs look like Git LFS pointer files."""
    for rel in required_source_files_for_version(source_contract_version):
        path = source_dir / rel
        if not path.is_file():
            continue
        try:
            head = path.read_bytes()[:200]
        except OSError as exc:
            raise ArchiveError(f"cannot read {rel}: {exc}") from exc
        if head.startswith(LFS_POINTER_PREFIX) or b"oid sha256:" in head[:120]:
            raise ArchiveError(
                f"Git LFS pointer detected in required file {rel}; "
                "PRD1C2 does not support LFS materialization"
            )


def detect_gitmodules(source_dir: Path) -> None:
    if (source_dir / ".gitmodules").exists():
        raise ArchiveError(
            "repository contains .gitmodules; submodules are unsupported in PRD1C2"
        )


def verify_required_files(
    source_dir: Path, *, source_contract_version: int
) -> dict[str, str]:
    abs_source = source_dir.resolve()
    for rel in required_source_files_for_version(source_contract_version):
        path = source_dir / rel
        if not path.exists():
            raise ArchiveError(f"required source path missing after extract: {rel}")
        if path.is_symlink():
            try:
                path.resolve().relative_to(abs_source)
            except ValueError as exc:
                raise ArchiveError(
                    f"required path symlink escapes snapshot: {rel}"
                ) from exc
    for rel in FORBIDDEN_SOURCE_PATHS:
        if (source_dir / rel).exists():
            raise ArchiveError(f"forbidden path present in snapshot: {rel}")
    hashes: dict[str, str] = {}
    for rel in CONTRACT_HASH_FILES:
        path = source_dir / rel
        if path.is_symlink():
            raise ArchiveError(f"contract hash path must not be a symlink: {rel}")
        data = path.read_bytes()
        hashes[rel] = hashlib.sha256(data).hexdigest()
    return hashes


def materialize_source(
    *,
    repo: Path,
    revision: str,
    incomplete_dir: Path,
    source_contract_version: int = CURRENT_PREPARE_SOURCE_CONTRACT_VERSION,
) -> MaterializedSource:
    """Archive, verify, extract into incomplete_dir/source."""
    rev = validate_full_sha(revision)
    source_dir = incomplete_dir / SOURCE_DIRNAME
    archive_path = incomplete_dir / f"{rev}.tar"
    incomplete_dir.mkdir(parents=True, exist_ok=True)
    try:
        stream_git_archive(repo, rev, archive_path)
        verify_archive_commit(archive_path, rev)
        tree_oid = resolve_tree_oid(repo, rev)
        extract_archive(archive_path, source_dir)
        detect_gitmodules(source_dir)
        detect_lfs_pointers(source_dir, source_contract_version=source_contract_version)
        hashes = verify_required_files(
            source_dir, source_contract_version=source_contract_version
        )
    except Exception:
        if archive_path.exists():
            archive_path.unlink(missing_ok=True)
        raise
    return MaterializedSource(
        revision=rev,
        tree_oid=tree_oid,
        source_dir=source_dir,
        archive_path=archive_path,
        contract_hashes=hashes,
    )
