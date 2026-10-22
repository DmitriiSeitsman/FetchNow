#!/usr/bin/env bash
# Disposable Linux acceptance harness for the media storage hard boundary.
#
# POLICY: PREPARED ONLY — DO NOT RUN in this SEC-08/09 local pass.
# Native enforcement: NOT_RUN until explicitly authorized.
#
# What it proves when run on a disposable Linux host/VM:
#   1) a small dedicated test FS (loop file + mount) is the only media write target
#   2) filling that FS yields ENOSPC/EDQUOT to a media-like writer
#   3) a sentinel on another FS remains writable
#   4) backing allocation does not grow past the prepared loop size
#   5) missing mount does not create/write under host root media paths
#   6) (operator-extended) cgroup ancestry for all three sidecars
#
# Test FS size is intentionally tiny and MUST NOT change production thresholds.
# Cleanup removes only exact resources created by this script (no global prune).
set -euo pipefail

echo "REFUSING TO RUN: native media storage acceptance harness is prepared-only." >&2
echo "Re-run with FETCHNOW_NATIVE_MEDIA_STORAGE_HARNESS_EXECUTE=1 after explicit authorization." >&2

if [[ "${FETCHNOW_NATIVE_MEDIA_STORAGE_HARNESS_EXECUTE:-}" != "1" ]]; then
  exit 78
fi

# --- Authorized execution path (still scoped & disposable) ---
if [[ -n "${FETCHNOW_NATIVE_MEDIA_STORAGE_WORKDIR+x}" ]]; then
  echo 'Refusing caller-supplied WORKDIR' >&2
  exit 2
fi
if [[ "$(uname -s)" != Linux || "$(id -u)" != 0 ]]; then
  echo 'Requires disposable Linux root host' >&2
  exit 2
fi
SIZE_MIB="${FETCHNOW_NATIVE_MEDIA_STORAGE_SIZE_MIB:-64}"
if [[ ! "$SIZE_MIB" =~ ^[0-9]+$ ]] || (( SIZE_MIB < 32 || SIZE_MIB > 256 )); then
  echo 'Test size must be 32..256 MiB' >&2
  exit 2
fi
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
WORKDIR="$(mktemp -d /tmp/fetchnow-media-storage-proof.XXXXXXXX)"
WORK_ID="$(stat -c '%d:%i:%u:%a' "$WORKDIR")"
LOOP_FILE="${WORKDIR}/media.img"
MNT="${WORKDIR}/mnt"
SENTINEL_DIR="${WORKDIR}/sentinel"
LOOP_DEV=''

cleanup() {
  if [[ ! "$WORKDIR" =~ ^/tmp/fetchnow-media-storage-proof\.[A-Za-z0-9]+$ || -L "$WORKDIR" ]]; then
    echo 'Cleanup refused: directory identity invalid' >&2
    return 1
  fi
  if [[ "$(stat -c '%d:%i:%u:%a' "$WORKDIR")" != "$WORK_ID" ]]; then
    echo 'Cleanup refused: directory replaced' >&2
    return 1
  fi
  if mountpoint -q "${MNT}" 2>/dev/null; then
    if [[ "$(findmnt -n -M "$MNT" -o SOURCE)" != "$LOOP_DEV" ]]; then
      echo 'Cleanup refused: mount source changed' >&2
      return 1
    fi
    if ! umount "$MNT"; then
      echo 'Cleanup failed: still mounted; NO deletion or detach' >&2
      return 1
    fi
  fi
  if [[ -n "${LOOP_DEV:-}" ]]; then
    if [[ "$(losetup --noheadings --output BACK-FILE "$LOOP_DEV")" != "$LOOP_FILE" ]]; then
      echo 'Cleanup refused: loop ownership changed' >&2
      return 1
    fi
    if ! losetup -d "$LOOP_DEV"; then
      echo 'Cleanup failed: detach failed; files retained' >&2
      return 1
    fi
  fi
  local mounts
  if ! mounts="$(findmnt -rn -o TARGET)"; then
    echo 'Cleanup refused: mount inventory failed' >&2
    return 1
  fi
  if grep -F -- "$WORKDIR/" <<<"$mounts"; then
    echo 'Cleanup refused: nested mount remains' >&2
    return 1
  fi
  rm -rf -- "$WORKDIR"
}
on_exit() {
  local rc=$?
  trap - EXIT
  if ! cleanup; then
    echo "CLEANUP_FAILED: retained $WORKDIR" >&2
    exit 1
  fi
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "${WORKDIR}" "${MNT}" "${SENTINEL_DIR}"
dd if=/dev/zero of="${LOOP_FILE}" bs=1M count="${SIZE_MIB}" status=none
LOOP_DEV="$(losetup --find --show "${LOOP_FILE}")"
mkfs.ext4 -q -F "${LOOP_DEV}"
mount "${LOOP_DEV}" "${MNT}"
mkdir -p "${MNT}/tmp" "${MNT}/executor" "${MNT}/media-net"

# Sentinel on a different FS (WORKDIR parent, typically root/tmp).
echo ok > "${SENTINEL_DIR}/before"

# Fill media FS until ENOSPC.
set +e
LC_ALL=C dd if=/dev/zero of="${MNT}/tmp/fill.bin" bs=1M 2>"${WORKDIR}/dd.err"
DD_RC=$?
set -e
if ! grep -qiE 'No space left|Disk quota exceeded' "${WORKDIR}/dd.err"; then
  echo "expected ENOSPC/EDQUOT from media FS writer" >&2
  exit 1
fi
echo still-ok > "${SENTINEL_DIR}/after"

# Backing file must not exceed prepared size (+small metadata slack).
ACTUAL="$(stat -c%s "${LOOP_FILE}")"
MAX=$((SIZE_MIB * 1024 * 1024 + 1048576))
if [[ "${ACTUAL}" -gt "${MAX}" ]]; then
  echo "loop backing grew beyond prepared boundary: ${ACTUAL} > ${MAX}" >&2
  exit 1
fi

# Invoke the real identity validator after unmount (no unrelated canary path).
umount "${MNT}"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SCRIPT_DIR" python3 - "$MNT" "$LOOP_DEV" <<'PY'
import sys
from pathlib import Path
from fetchnow_release.media_storage import (
    MediaStorageError, assert_path_on_contract_fs,
    disposable_test_storage_contract, parse_media_storage_contract,
)
root = Path(sys.argv[1])
contract = parse_media_storage_contract(disposable_test_storage_contract(
    host_bind_root=str(root), source_device=sys.argv[2],
))
try:
    assert_path_on_contract_fs(root, contract, label="missing-mount")
except MediaStorageError:
    pass
else:
    raise SystemExit("missing mount was not rejected")
PY
echo "FILESYSTEM_COMPONENT_PASS dd_rc=${DD_RC} loop_bytes=${ACTUAL}; Compose/cgroup NOT_RUN"
exit 78
