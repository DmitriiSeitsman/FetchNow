"""Offline regressions for readiness, containment oracles and evidence export."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import container_supervisor as supervisor  # noqa: E402
from cgroup_contract import (  # noqa: E402
    cgroup_boundary_errors,
    private_cgroup_mount,
    status_caps_are_narrow,
)
from container_cgroup_proof import Proof  # noqa: E402
from export_evidence import export_evidence  # noqa: E402

MOUNTINFO = (
    "36 25 0:30 / /sys/fs/cgroup rw,nosuid,nodev,noexec - cgroup2 cgroup2 rw,nsdelegate\n"
    "37 25 0:31 / /sys rw - sysfs sysfs rw\n"
)
CAPS = {name: "00000000000001c0" for name in ("CapEff", "CapPrm", "CapBnd")}


def boundary() -> tuple[dict, dict]:
    host: dict = {
        "root_identity": {"dev": 30, "ino": 1234},
        "namespace": "cgroup:[12345]",
        "host_namespace": "cgroup:[11111]",
    }
    inside = {
        "cgroup": "0::/",
        "mountinfo_all": MOUNTINFO,
        "cgroup_root_identity": dict(host["root_identity"]),
        "cgroup_namespace": host["namespace"],
        "inherited_cgroup_fds": [],
        "outside": {
            "sentinel": "errno 2", "slice": "errno 2",
            "mount_parent_resolved": "/sys/fs",
        },
    }
    return inside, host


class ReadinessTests(unittest.TestCase):
    def test_status_read_only_after_all_tool_pid_barriers(self) -> None:
        events = []

        def ready(path, timeout):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 5)
            events.append(path.name)
            return True

        def status(_path):
            self.assertEqual(events, ["parent.pid", "child.pid"] * 2)
            return CAPS

        with patch.object(supervisor, "wait_file", side_effect=ready), patch.object(
            supervisor, "status_caps", side_effect=status
        ):
            self.assertEqual(supervisor.ready_launcher_caps(), {"a": CAPS, "b": CAPS})

    def test_timeout_does_not_read_or_accept_caps(self) -> None:
        with patch.object(supervisor, "wait_file", return_value=False), patch.object(
            supervisor, "status_caps"
        ) as read:
            with self.assertRaises(TimeoutError):
                supervisor.ready_launcher_caps()
            read.assert_not_called()

    def test_elapsed_budget_never_starts_a_new_wait(self) -> None:
        with patch.object(supervisor.time, "monotonic", side_effect=[0, 6]), patch.object(
            supervisor, "wait_file"
        ) as wait:
            with self.assertRaises(TimeoutError):
                supervisor.ready_launcher_caps()
            wait.assert_not_called()

    def test_missing_or_partial_caps_still_fail(self) -> None:
        self.assertTrue(status_caps_are_narrow(CAPS))
        for value in (None, {}, {"CapEff": "1c0"}, {**CAPS, "CapBnd": "garbage"},
                      {**CAPS, "CapPrm": "1c1"}, {**CAPS, "CapBnd": None}):
            with self.subTest(value=value):
                self.assertFalse(status_caps_are_narrow(value))

    def test_evaluator_cannot_accept_missing_a_b_or_probe_status(self) -> None:
        for name in ("a", "b", "probe"):
            proof = Proof()
            maps = {key: CAPS for key in ("a", "b", "probe") if key != name}
            proof.evaluate({"supervisor_caps": CAPS, "launcher_caps": maps})
            check = next(c for c in proof.checks if c["name"] == "narrow_capabilities_without_chown")
            self.assertEqual(check["status"], "FAIL")

    def test_no_runtime_account_database_mutation(self) -> None:
        source = pathlib.Path(supervisor.__file__).read_text()
        self.assertNotIn('"useradd"', source)
        self.assertNotIn('"groupadd"', source)


class BoundaryTests(unittest.TestCase):
    def test_parent_mount_is_not_parent_host_cgroup(self) -> None:
        inside, host = boundary()
        inside["outside"]["dotdot_same_as_root"] = False  # old run observation
        self.assertEqual(cgroup_boundary_errors(inside, host), [])

    def test_missing_fields_fail_closed(self) -> None:
        good, host = boundary()
        for key in good:
            value = copy.deepcopy(good)
            del value[key]
            with self.subTest(key=key):
                self.assertTrue(cgroup_boundary_errors(value, host))
        self.assertTrue(cgroup_boundary_errors(good, {}))

    def test_wrong_inode_device_or_namespace_fails(self) -> None:
        good, host = boundary()
        for key, value in (
            ("cgroup_root_identity", {"dev": 30, "ino": 999}),
            ("cgroup_root_identity", {"dev": 31, "ino": 1234}),
            ("cgroup_namespace", host["host_namespace"]),
            ("inherited_cgroup_fds", [8]),
        ):
            with self.subTest(key=key):
                self.assertTrue(cgroup_boundary_errors({**good, key: value}, host))
        same = {**host, "host_namespace": host["namespace"]}
        self.assertTrue(cgroup_boundary_errors(good, same))

    def test_open_or_unexpected_errno_is_not_denial(self) -> None:
        inside, host = boundary()
        for value in ("opened", "unset", "errno 5", "errno 24", "errno 2 extra", ""):
            for key in ("slice", "sentinel"):
                changed = copy.deepcopy(inside)
                changed["outside"][key] = value
                with self.subTest(value=value, key=key):
                    self.assertTrue(cgroup_boundary_errors(changed, host))

    def test_mount_oracle_rejects_alternate_bind_and_wrong_root(self) -> None:
        self.assertTrue(private_cgroup_mount(MOUNTINFO))
        invalid = (
            "", "broken", MOUNTINFO + MOUNTINFO.splitlines()[0] + "\n",
            MOUNTINFO.replace(" / /sys/fs/cgroup", " /host /sys/fs/cgroup"),
            MOUNTINFO.replace("/sys/fs/cgroup rw", "/sys/fs/cgroup ro"),
            MOUNTINFO.replace("rw,nsdelegate", "rw"),
            MOUNTINFO.replace("cgroup2 cgroup2", "cgroup cgroup"),
        )
        for text in invalid:
            with self.subTest(text=text):
                self.assertFalse(private_cgroup_mount(text))

    def test_evaluate_uses_new_boundary_and_exact_directory_inventory(self) -> None:
        inside, host = boundary()
        inside["cgroup_dirs"] = ["fn-a", "fn-b", "fn-probe"]
        proof = Proof()
        proof.evidence["executor_scope"] = host
        proof.evaluate(inside)
        checks = {c["name"]: c["status"] for c in proof.checks}
        self.assertEqual(checks["supervisor_cannot_open_outside_subtree"], "PASS")
        self.assertEqual(checks["container_view_has_no_sentinel_scope"], "PASS")
        for dirs in (None, [], ["fn-a", "fn-b", "fn-probe", "unrelated"]):
            proof = Proof()
            proof.evaluate({**inside, "cgroup_dirs": dirs})
            check = next(c for c in proof.checks if c["name"] == "container_view_has_no_sentinel_scope")
            self.assertEqual(check["status"], "FAIL")


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.source = self.root / "proof"
        self.destination = self.root / "upload"
        self.source.mkdir()
        result = b'{"kind":"sec08-container-cgroup-proof","conclusion":"FAIL"}\n'
        (self.source / "result.json").write_bytes(result)
        (self.source / "stage-hashes.json").write_text(json.dumps({
            "result.json": hashlib.sha256(result).hexdigest(),
        }))
        (self.source / "inside").mkdir()
        self.status = self.source / "inside/a-launcher-status.txt"
        self.status.write_text("CapEff:\t00000000000001c0\n")
        self.status.chmod(0o640)

    def test_copy_is_readable_bytes_identical_original_mode_preserved(self) -> None:
        manifest = export_evidence(self.source, self.destination)
        for relative, entry in manifest.items():
            original = self.source / relative
            copy_file = self.destination / relative
            self.assertEqual(original.read_bytes(), copy_file.read_bytes())
            self.assertEqual(hashlib.sha256(copy_file.read_bytes()).hexdigest(), entry["sha256"])
            self.assertEqual(stat.S_IMODE(copy_file.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.status.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((self.destination / "inside").stat().st_mode), 0o755)
        # Export never converts the recorded failure into success.
        self.assertEqual(json.loads((self.destination / "result.json").read_text())["conclusion"], "FAIL")

    def test_rejects_file_and_directory_symlinks(self) -> None:
        self.status.unlink()
        self.status.symlink_to(self.source / "result.json")
        with self.assertRaises(OSError):
            export_evidence(self.source, self.destination)
        self.assertFalse(self.destination.exists())
        self.status.unlink()
        (self.source / "inside").rmdir()
        (self.source / "inside").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            export_evidence(self.source, self.destination)

    def test_rejects_fifo_without_blocking(self) -> None:
        self.status.unlink()
        os.mkfifo(self.status)
        with self.assertRaises(ValueError):
            export_evidence(self.source, self.destination)

    def test_corrupt_original_hash_fails_before_copy(self) -> None:
        (self.source / "result.json").write_text('{"kind":"sec08-container-cgroup-proof"}')
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            export_evidence(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_missing_hash_inventory_or_result_fails(self) -> None:
        (self.source / "stage-hashes.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "inventory"):
            export_evidence(self.source, self.destination)
        (self.source / "result.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            export_evidence(self.source, self.destination)

    def test_unrelated_files_and_existing_destination_refused(self) -> None:
        private = self.source / ".env"
        private.write_text("must not be exported")
        with self.assertRaisesRegex(ValueError, "unexpected evidence"):
            export_evidence(self.source, self.destination)
        private.unlink()
        self.destination.mkdir()
        with self.assertRaisesRegex(ValueError, "must not exist"):
            export_evidence(self.source, self.destination)

    def test_stage_hashes_and_compile_binary_exclusion(self) -> None:
        (self.source / "stages").mkdir()
        stage = b'{"conclusion":"FAIL"}\n'
        (self.source / "stages/cleanup.json").write_bytes(stage)
        hashes = json.loads((self.source / "stage-hashes.json").read_text())
        hashes["cleanup.json"] = hashlib.sha256(stage).hexdigest()
        (self.source / "stage-hashes.json").write_text(json.dumps(hashes))
        (self.source / "bin").mkdir()
        (self.source / "bin/sec08tool").write_bytes(b"not uploaded")
        manifest = export_evidence(self.source, self.destination)
        self.assertIn("stages/cleanup.json", manifest)
        self.assertFalse((self.destination / "bin").exists())

    def test_required_upload_cannot_ignore_failure(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[2]
        workflow = (root / ".github/workflows/sec08-native-feasibility.yml").read_text()
        self.assertNotIn("continue-on-error", workflow)
        self.assertIn("export_evidence.py", workflow)
        self.assertIn("-m unittest discover", workflow)

    @unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0, "requires disposable Linux root")
    def test_nonroot_can_read_copy_but_not_root_owned_original(self) -> None:
        # Reproduce the artifact uploader's DAC failure without weakening the
        # original or the tool workspace. This is a separate root-only fixture.
        self.root.chmod(0o755)
        self.source.chmod(0o755)
        (self.source / "inside").chmod(0o755)

        def reader(uid_path):
            return subprocess.run(
                [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).read_bytes()", str(uid_path)],
                user=10001, group=10001, extra_groups=[],
                capture_output=True, timeout=5, check=False,
            )

        self.assertNotEqual(reader(self.status).returncode, 0)
        export_evidence(self.source, self.destination)
        for path in self.destination.rglob("*"):
            if path.is_file():
                self.assertEqual(reader(path).returncode, 0, path.name)
        self.assertNotEqual(reader(self.status).returncode, 0)


if __name__ == "__main__":
    unittest.main()
