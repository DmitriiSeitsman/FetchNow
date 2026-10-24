"""Read-only validation of single-image, untagged Docker-save archives.

Nothing is extracted. Archive checksums are transport integrity, not a security
approval or signed build provenance. Docker remains responsible for validating
layer/diff-ID consistency when loading an authenticated bundle.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PLATFORM = "linux/amd64"
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_METADATA_BYTES = 2 * 1024**2
MAX_ARCHIVE_MEMBERS = 20000


class ImageArchiveError(ValueError):
    """Unsupported or inconsistent image archive."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ImageArchiveError("duplicate JSON key")
        result[key] = value
    return result


def parse_json(payload: bytes) -> Any:
    if len(payload) > MAX_METADATA_BYTES:
        raise ImageArchiveError("JSON metadata exceeds limit")
    return json.loads(payload, object_pairs_hook=_unique_pairs)


def _safe_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or ":" in name
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise ImageArchiveError("unsafe archive member name")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or str(path) != name or name == ".":
        raise ImageArchiveError("non-canonical archive member name")
    return name


def validate_image_archive(path: Path, *, image_id: str, revision: str | None) -> None:
    """Verify one platform/config and no tags before Docker may see the tar."""
    if not IMAGE_ID.fullmatch(image_id):
        raise ImageArchiveError("invalid expected image ID")
    if (
        path.is_symlink()
        or not path.is_file()
        or not 0 < path.stat().st_size <= MAX_ARCHIVE_BYTES
    ):
        raise ImageArchiveError("missing, unsafe or oversized image archive")
    try:
        with tarfile.open(path, mode="r:") as tf:
            members: dict[str, tarfile.TarInfo] = {}
            total = 0
            for member in tf:
                if len(members) >= MAX_ARCHIVE_MEMBERS:
                    raise ImageArchiveError("too many archive members")
                name = _safe_name(
                    member.name.rstrip("/") if member.isdir() else member.name
                )
                if name in members or not (member.isdir() or member.isfile()):
                    raise ImageArchiveError(
                        "duplicate member or non-regular archive node"
                    )
                if member.size < 0:
                    raise ImageArchiveError("negative member size")
                total += member.size
                if total > MAX_ARCHIVE_BYTES:
                    raise ImageArchiveError("archive contents exceed limit")
                members[name] = member

            def read(name: object) -> bytes:
                key = _safe_name(name)
                member = members.get(key)
                if (
                    member is None
                    or not member.isfile()
                    or member.size > MAX_METADATA_BYTES
                ):
                    raise ImageArchiveError("missing or oversized metadata member")
                fh = tf.extractfile(member)
                if fh is None:
                    raise ImageArchiveError("cannot read metadata member")
                with fh:
                    data = fh.read(MAX_METADATA_BYTES + 1)
                if len(data) != member.size:
                    raise ImageArchiveError("truncated metadata member")
                return data

            manifest = parse_json(read("manifest.json"))
            if (
                not isinstance(manifest, list)
                or len(manifest) != 1
                or not isinstance(manifest[0], dict)
            ):
                raise ImageArchiveError("archive must contain exactly one image")
            entry = manifest[0]
            if entry.get("RepoTags") not in (None, []):
                raise ImageArchiveError("image archives must not contain tags")
            config_bytes = read(entry.get("Config"))
            if "sha256:" + hashlib.sha256(config_bytes).hexdigest() != image_id:
                raise ImageArchiveError(
                    "archive config digest differs from expected Image ID"
                )
            config = parse_json(config_bytes)
            if (
                not isinstance(config, dict)
                or f"{config.get('os')}/{config.get('architecture')}" != PLATFORM
            ):
                raise ImageArchiveError("archive is not linux/amd64")
            if revision is not None:
                cfg = config.get("config")
                labels = cfg.get("Labels") if isinstance(cfg, dict) else None
                if (
                    not isinstance(labels, dict)
                    or labels.get("org.opencontainers.image.revision") != revision
                ):
                    raise ImageArchiveError("archive OCI revision mismatch")
            layers = entry.get("Layers")
            if not isinstance(layers, list) or not layers:
                raise ImageArchiveError("image layers missing")
            for layer in layers:
                layer_member = members.get(_safe_name(layer))
                if layer_member is None or not layer_member.isfile():
                    raise ImageArchiveError("image layer missing")
            # Containerd's Docker-save format also carries an OCI index. Refuse
            # named tags and any secondary config/platform hidden in that graph.
            if "index.json" in members:
                seen: set[str] = set()

                def visit(doc: object, depth: int) -> None:
                    if depth > 4 or not isinstance(doc, dict):
                        raise ImageArchiveError("invalid OCI descriptor graph")
                    descriptors = doc.get("manifests")
                    if isinstance(descriptors, list) and descriptors:
                        for desc in descriptors:
                            if not isinstance(desc, dict):
                                raise ImageArchiveError("invalid OCI descriptor")
                            annotations = desc.get("annotations", {})
                            if not isinstance(annotations, dict):
                                raise ImageArchiveError("invalid OCI annotations")
                            for key in (
                                "org.opencontainers.image.ref.name",
                                "io.containerd.image.name",
                            ):
                                if key in annotations and annotations[key] not in (
                                    image_id,
                                    image_id[7:],
                                ):
                                    raise ImageArchiveError(
                                        "tag hidden in OCI descriptor"
                                    )
                            platform = desc.get("platform")
                            if platform is not None and (
                                not isinstance(platform, dict)
                                or f"{platform.get('os')}/{platform.get('architecture')}"
                                != PLATFORM
                            ):
                                raise ImageArchiveError("secondary OCI platform")
                            digest = desc.get("digest")
                            if (
                                not isinstance(digest, str)
                                or not IMAGE_ID.fullmatch(digest)
                                or digest in seen
                            ):
                                raise ImageArchiveError(
                                    "invalid or repeated OCI digest"
                                )
                            seen.add(digest)
                            payload = read("blobs/sha256/" + digest[7:])
                            if hashlib.sha256(payload).hexdigest() != digest[7:]:
                                raise ImageArchiveError("OCI descriptor hash mismatch")
                            visit(parse_json(payload), depth + 1)
                        return
                    cfg = doc.get("config")
                    if not isinstance(cfg, dict) or cfg.get("digest") != image_id:
                        raise ImageArchiveError("secondary OCI config")

                visit(parse_json(read("index.json")), 0)
    except (tarfile.TarError, UnicodeError, json.JSONDecodeError) as exc:
        raise ImageArchiveError("malformed image archive") from exc
