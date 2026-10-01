"""Host-independent checks for the container cgroup proof preflight."""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from cgroup_contract import (  # noqa: E402
    engine_at_least,
    memory_max_is_64m,
    narrow_caps_only,
    preflight_blockers,
    security_options_are_rootless,
)


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


if __name__ == "__main__":
    unittest.main()
