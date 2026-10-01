"""Host-independent checks for the container cgroup proof preflight."""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cgroup_contract import (  # noqa: E402
    capabilities_are_exact,
    cgroup2_mount_points,
    control_groups_match,
    engine_at_least,
    flattened_unit_cgroup_dir,
    memory_max_is_64m,
    narrow_caps_only,
    observe_slice_memory_max,
    preflight_blockers,
    proc_cgroup_under_control_group,
    resolve_control_group_dir,
    security_options_are_rootless,
)
from workspace_contract import dac_allows, mkdir_visible_mode  # noqa: E402


def facts(**overrides: object) -> dict:
    base = {
        "machine": "x86_64",
        "system": "Linux",
        "cgroup_v2": True,
        "docker_server_version": "28.0.1",
        "cgroup_driver": "systemd",
        "systemd": True,
        "rootful": True,
        "security_options": ["name=seccomp,profile=builtin", "name=cgroupns"],
        "landlock_abi": 7,
    }
    base.update(overrides)
    return base


class ContractTests(unittest.TestCase):
    def test_ready_preflight_has_no_blockers(self) -> None:
        self.assertEqual(preflight_blockers(facts()), [])

    def test_cgroupfs_driver_blocks_even_on_v2_and_docker_28(self) -> None:
        blockers = preflight_blockers(facts(cgroup_driver="cgroupfs"))
        self.assertEqual(blockers, ["cgroup driver is not systemd"])

    def test_old_engine_blocks(self) -> None:
        self.assertFalse(engine_at_least("27.5.1"))
        self.assertTrue(engine_at_least("28.0.0"))
        self.assertIn("docker engine is not >= 28.0.0", preflight_blockers(facts(docker_server_version="27.5.1")))

    def test_rootless_blocks(self) -> None:
        self.assertTrue(security_options_are_rootless(["name=rootless"]))
        blockers = preflight_blockers(facts(security_options=["name=rootless"]))
        self.assertIn("docker is not rootful", blockers)

    def test_low_abi_blocks(self) -> None:
        self.assertIn("landlock abi is below 6", preflight_blockers(facts(landlock_abi=5)))

    def test_narrow_caps_reject_chown_and_sys_admin(self) -> None:
        self.assertTrue(narrow_caps_only("00000000000001c0"))
        self.assertFalse(narrow_caps_only("00000000000001c1"))
        self.assertFalse(narrow_caps_only("00000000002001c0"))

    def test_memory_max_accepts_bytes_or_systemd_suffix(self) -> None:
        self.assertTrue(memory_max_is_64m("67108864"))
        self.assertTrue(memory_max_is_64m("64M"))
        self.assertFalse(memory_max_is_64m("max"))

    def test_supervisor_source_does_not_call_chown(self) -> None:
        text = pathlib.Path(__file__).with_name("container_supervisor.py").read_text()
        self.assertNotIn("os.chown", text)
        self.assertNotIn("os.lchown", text)
        self.assertNotIn("shutil.chown", text)


MOUNTINFO = (
    "36 25 0:30 / /sys/fs/cgroup rw,nosuid,nodev,noexec shared:9 "
    "- cgroup2 cgroup2 rw,nsdelegate\n"
    "37 25 0:31 / /sys rw - sysfs sysfs rw\n"
)
NESTED = (
    "/fetchnow.slice/fetchnow-sec08.slice/fetchnow-sec08-cgproof.slice/"
    "fetchnow-sec08-cgproof-961a2168.slice"
)
UNIT = "fetchnow-sec08-cgproof-961a2168.slice"


class SlicePathTests(unittest.TestCase):
    def test_simple_slice_uses_control_group(self) -> None:
        observed, error = observe_slice_memory_max(
            active_state="active",
            control_group="/simple.slice",
            cgroup2_mounts=cgroup2_mount_points(MOUNTINFO),
            memory_max_text="67108864",
        )
        self.assertEqual(error, "")
        self.assertEqual(observed["host_dir"], "/sys/fs/cgroup/simple.slice")
        self.assertEqual(observed["memory_max_path"], "/sys/fs/cgroup/simple.slice/memory.max")
        self.assertEqual(observed["memory_max"], "67108864")

    def test_dashed_slice_is_not_the_flattened_unit_path(self) -> None:
        mounts = ["/sys/fs/cgroup"]
        observed, error = observe_slice_memory_max(
            active_state="active",
            control_group=NESTED,
            cgroup2_mounts=mounts,
            memory_max_text="67108864",
        )
        flat = flattened_unit_cgroup_dir("/sys/fs/cgroup", UNIT)
        self.assertEqual(flat, "/sys/fs/cgroup/" + UNIT)
        self.assertEqual(error, "")
        self.assertNotEqual(observed["host_dir"], flat)
        self.assertEqual(observed["host_dir"], "/sys/fs/cgroup" + NESTED)
        self.assertFalse(observed["memory_max_path"].startswith(flat + "/"))

    def test_empty_control_group_fails(self) -> None:
        _observed, error = observe_slice_memory_max(
            active_state="active",
            control_group="   ",
            cgroup2_mounts=["/sys/fs/cgroup"],
            memory_max_text="67108864",
        )
        self.assertEqual(error, "control group is empty")

    def test_root_and_traversal_fail(self) -> None:
        for control, expected in (
            ("/", "control group is the cgroup root"),
            (".", "control group is the cgroup root"),
            ("/foo/../../etc", "control group is not a contained path"),
            ("../simple.slice", "control group is not a contained path"),
            ("simple.slice", "control group is not a contained path"),
            ("/foo/../bar.slice", "control group is not a contained path"),
        ):
            directory, error = resolve_control_group_dir(control, "/sys/fs/cgroup")
            self.assertEqual(directory, "", control)
            self.assertEqual(error, expected, control)

    def test_missing_memory_max_is_not_replaced_by_a_property(self) -> None:
        _observed, error = observe_slice_memory_max(
            active_state="active",
            control_group="/simple.slice",
            cgroup2_mounts=["/sys/fs/cgroup"],
            memory_max_text=None,
        )
        self.assertEqual(error, "memory.max is missing")
        _observed, error = observe_slice_memory_max(
            active_state="active",
            control_group="/simple.slice",
            cgroup2_mounts=["/sys/fs/cgroup"],
            memory_max_text="max",
        )
        self.assertEqual(error, "memory.max is not 64M")

    def test_inactive_slice_fails_before_a_path_is_accepted(self) -> None:
        _observed, error = observe_slice_memory_max(
            active_state="inactive",
            control_group="/simple.slice",
            cgroup2_mounts=["/sys/fs/cgroup"],
            memory_max_text="67108864",
        )
        self.assertEqual(error, "slice is not active")

    def test_ambiguous_cgroup2_mount_fails(self) -> None:
        _observed, error = observe_slice_memory_max(
            active_state="active",
            control_group="/simple.slice",
            cgroup2_mounts=["/sys/fs/cgroup", "/sys/fs/cgroup/unified"],
            memory_max_text="67108864",
        )
        self.assertEqual(error, "cgroup2 mount is not confirmed")

    def test_authoritative_path_must_stay_the_same(self) -> None:
        self.assertTrue(control_groups_match(NESTED, NESTED))
        self.assertFalse(control_groups_match(NESTED, "/simple.slice"))
        self.assertFalse(control_groups_match("", ""))
        self.assertFalse(control_groups_match("  ", NESTED))

    def test_proc_path_must_stay_under_the_control_group(self) -> None:
        self.assertTrue(
            proc_cgroup_under_control_group("0::" + NESTED + "/docker-abc.scope", NESTED)
        )
        self.assertFalse(proc_cgroup_under_control_group("0::/other.slice/docker-abc.scope", NESTED))

    def test_proof_source_does_not_flatten_the_unit_name(self) -> None:
        text = pathlib.Path(__file__).with_name("container_cgroup_proof.py").read_text()
        self.assertNotIn('"/sys/fs/cgroup" / self.slice_name', text)
        self.assertNotIn("/sys/fs/cgroup/{self.slice_name}", text)
        self.assertNotIn('f"SLICE_MEMORY_PATH=', text)
        self.assertIn('f"HOST_SLICE_MEMORY_PATH=', text)
        self.assertIn("namespace_cgroup_root", pathlib.Path(__file__).with_name("container_supervisor.py").read_text())


class CapabilityAndDacTests(unittest.TestCase):
    def test_prefixed_and_unprefixed_caps_are_the_exact_set(self) -> None:
        prefixed = ["CAP_SETGID", "CAP_SETPCAP", "CAP_SETUID"]
        unprefixed = ["SETUID", "SETGID", "SETPCAP"]
        self.assertTrue(capabilities_are_exact(prefixed))
        self.assertTrue(capabilities_are_exact(unprefixed))
        self.assertTrue(capabilities_are_exact(["cap_setuid", "Cap_Setgid", "setpcap"]))

    def test_extra_or_missing_capability_is_not_exact(self) -> None:
        self.assertFalse(capabilities_are_exact(["CAP_SETUID", "CAP_SETGID", "CAP_SETPCAP", "CAP_CHOWN"]))
        self.assertFalse(capabilities_are_exact(["CAP_SETUID", "CAP_SETGID"]))
        self.assertFalse(capabilities_are_exact(["SETUID", "SETGID", "SETPCAP", "DAC_OVERRIDE"]))

    def test_malformed_capability_list_is_not_pass(self) -> None:
        self.assertFalse(capabilities_are_exact(None))
        self.assertFalse(capabilities_are_exact("CAP_SETUID"))
        self.assertFalse(capabilities_are_exact([]))
        self.assertFalse(capabilities_are_exact(["CAP_SETUID", None, "CAP_SETGID"]))
        self.assertFalse(capabilities_are_exact(["CAP_"]))
        self.assertFalse(capabilities_are_exact(["SETUID SETGID"]))

    def test_in_container_hex_is_separate_from_inspect_names(self) -> None:
        self.assertTrue(narrow_caps_only("00000000000001c0"))
        self.assertFalse(capabilities_are_exact(["00000000000001c0"]))
        with self.assertRaises(ValueError):
            narrow_caps_only("CAP_SETUID")

    def test_uid_zero_does_not_bypass_attempt_directory_dac(self) -> None:
        attempt = {"owner": 10003, "group": 10001, "mode": 0o750}
        self.assertFalse(
            dac_allows(euid=0, egid=0, groups=[], access="x", **attempt)
        )
        self.assertTrue(
            dac_allows(euid=0, egid=10001, groups=[], access="x", **attempt)
        )
        self.assertTrue(
            dac_allows(euid=10003, egid=10003, groups=[], access="w", owner=10003, group=10001, mode=0o2750)
        )
        self.assertTrue(
            dac_allows(euid=10001, egid=10001, groups=[], access="r", owner=10003, group=10001, mode=0o640)
        )
        self.assertFalse(
            dac_allows(euid=10003, egid=10003, groups=[], access="w", owner=10002, group=10001, mode=0o660)
        )

    def test_mkdir_drops_setgid_so_requested_mode_is_not_proven(self) -> None:
        self.assertEqual(mkdir_visible_mode(0o2750), 0o750)
        self.assertNotEqual(mkdir_visible_mode(0o2750), 0o2750)


if __name__ == "__main__":
    unittest.main()
