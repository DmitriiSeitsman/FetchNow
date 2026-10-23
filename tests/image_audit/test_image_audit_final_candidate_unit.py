from __future__ import annotations

import argparse
import io
import json
import struct
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import image_audit_final_caller_attribution as attr  # noqa: E402
import image_audit_final_candidate as fin  # noqa: E402
import image_audit_final_reconcile as rec  # noqa: E402

import image_audit as ia  # noqa: E402

WORKFLOW = ROOT / ".github/workflows/sec08-09-final-candidate-image-audit.yml"
SOURCE = fin.AUDITED_SOURCE_SHA
PREDECESSOR = fin.AUDIT_PREDECESSOR_SHA
HEAD = "a" * 40


# --- authorization ---------------------------------------------------------------


def _auth(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "event_name": "push",
        "run_attempt": "1",
        "ref": fin.BRANCH_REF,
        "github_sha": HEAD,
        "head_sha": HEAD,
        "parent_shas": [PREDECESSOR],
        "changed_paths": list(fin.AUDIT_MANIFEST),
        "predecessor_parent_shas": [SOURCE],
        "predecessor_changed_paths": list(fin.INITIAL_AUDIT_MANIFEST),
        "cumulative_changed_paths": list(fin.CUMULATIVE_AUDIT_MANIFEST),
    }
    kwargs.update(overrides)
    return fin.authorize(**kwargs)


def test_authorize_exact_successor_chain_without_retargeting_source() -> None:
    result = _auth()
    assert result["allowed"] is True
    assert result["audited_source_sha"] == SOURCE
    assert result["audit_predecessor_sha"] == PREDECESSOR
    assert result["predecessor_parents"] == [SOURCE]
    assert result["audit_harness_sha"] == HEAD


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"event_name": "workflow_dispatch"}, "event_not_push"),
        ({"run_attempt": "2"}, "run_attempt_not_1"),
        ({"ref": "refs/heads/main"}, "ref_mismatch"),
        ({"github_sha": "b" * 40}, "head_not_github_sha"),
        ({"parent_shas": ["c" * 40]}, "parent_not_audit_predecessor"),
        ({"parent_shas": [SOURCE]}, "parent_not_audit_predecessor"),
        ({"parent_shas": [PREDECESSOR, "d" * 40]}, "parent_not_audit_predecessor"),
        ({"predecessor_parent_shas": []}, "predecessor_not_audited_source"),
        ({"predecessor_parent_shas": ["c" * 40]}, "predecessor_not_audited_source"),
        (
            {"predecessor_parent_shas": [SOURCE, "d" * 40]},
            "predecessor_not_audited_source",
        ),
        (
            {
                "predecessor_changed_paths": [
                    *fin.INITIAL_AUDIT_MANIFEST,
                    "backend/Dockerfile",
                ]
            },
            "predecessor_paths_not_exact_manifest",
        ),
        (
            {"predecessor_changed_paths": list(fin.INITIAL_AUDIT_MANIFEST[:-1])},
            "predecessor_paths_not_exact_manifest",
        ),
        (
            {
                "cumulative_changed_paths": [
                    *fin.CUMULATIVE_AUDIT_MANIFEST,
                    "backend/uv.lock",
                ]
            },
            "cumulative_paths_not_exact_manifest",
        ),
        (
            {"cumulative_changed_paths": list(fin.CUMULATIVE_AUDIT_MANIFEST[:-1])},
            "cumulative_paths_not_exact_manifest",
        ),
        (
            {
                "cumulative_changed_paths": [
                    *fin.CUMULATIVE_AUDIT_MANIFEST,
                    fin.CUMULATIVE_AUDIT_MANIFEST[0],
                ]
            },
            "cumulative_paths_not_exact_manifest",
        ),
        (
            {"changed_paths": [*fin.AUDIT_MANIFEST, "backend/Dockerfile"]},
            "changed_paths_not_exact_manifest",
        ),
        (
            {"changed_paths": list(fin.AUDIT_MANIFEST[:-1])},
            "changed_paths_not_exact_manifest",
        ),
        (
            {"changed_paths": [*fin.AUDIT_MANIFEST, fin.AUDIT_MANIFEST[0]]},
            "changed_paths_not_exact_manifest",
        ),
    ],
)
def test_authorize_rejects(overrides: dict[str, Any], reason: str) -> None:
    result = _auth(**overrides)
    assert result["allowed"] is False
    assert reason in result["reasons"]


def test_manifest_excludes_dirty_preserved_files() -> None:
    manifest = set(fin.AUDIT_MANIFEST)
    assert ".github/workflows/sec03c-candidate7-image-audit.yml" not in manifest
    assert not any(
        "residual" in p or "gosu" in p or "storage_resource" in p for p in manifest
    )
    assert ".github/workflows/sec08-09-final-candidate-image-audit.yml" in manifest
    assert all((ROOT / p).is_file() for p in manifest)


def test_pinned_predecessor_is_the_original_audit_only_commit() -> None:
    parents = fin._git(ROOT, "rev-list", "--parents", "-n", "1", PREDECESSOR)
    assert parents.split() == [PREDECESSOR, SOURCE]
    paths = fin._git(
        ROOT, "diff", "--name-only", "--no-renames", SOURCE, PREDECESSOR
    ).splitlines()
    assert sorted(paths) == sorted(fin.INITIAL_AUDIT_MANIFEST)
    assert len(fin.AUDIT_MANIFEST) == 6
    assert len(fin.CUMULATIVE_AUDIT_MANIFEST) == 8


def test_cmd_authorize_reads_commit_chain_not_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = {
        ("rev-parse", "HEAD"): HEAD + "\n",
        ("rev-list", "--parents", "-n", "1", "HEAD"): f"{HEAD} {PREDECESSOR}\n",
        ("diff", "--name-only", "--no-renames", PREDECESSOR, HEAD): "\n".join(
            fin.AUDIT_MANIFEST
        )
        + "\n",
        ("rev-list", "--parents", "-n", "1", PREDECESSOR): f"{PREDECESSOR} {SOURCE}\n",
        ("diff", "--name-only", "--no-renames", SOURCE, PREDECESSOR): "\n".join(
            fin.INITIAL_AUDIT_MANIFEST
        )
        + "\n",
        ("diff", "--name-only", "--no-renames", SOURCE, HEAD): "\n".join(
            fin.CUMULATIVE_AUDIT_MANIFEST
        )
        + "\n",
    }
    calls: list[tuple[str, ...]] = []

    def read_git(repo: Path, *args: str) -> str:
        assert repo == ROOT
        calls.append(args)
        return outputs[args]

    monkeypatch.setattr(fin, "_git", read_git)
    for key, value in {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REF": fin.BRANCH_REF,
        "GITHUB_SHA": HEAD,
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
    }.items():
        monkeypatch.setenv(key, value)
    out = tmp_path / "authorize.json"
    assert fin.cmd_authorize(argparse.Namespace(repo=ROOT, json_out=out)) == 0
    result = json.loads(out.read_text())
    assert result["allowed"] is True
    assert result["audited_source_sha"] == SOURCE
    assert set(calls) == set(outputs)
    assert (tmp_path / "output.txt").read_text() == "allowed=true\n"

    outputs[("diff", "--name-only", "--no-renames", SOURCE, HEAD)] += (
        "backend/Dockerfile\n"
    )
    assert fin.cmd_authorize(argparse.Namespace(repo=ROOT, json_out=out)) == 0
    rejected = json.loads(out.read_text())
    assert rejected["allowed"] is False
    assert "cumulative_paths_not_exact_manifest" in rejected["reasons"]


# --- source proof / DB freeze / summary --------------------------------------------


def test_compare_runtime_inputs() -> None:
    assert fin.compare_runtime_inputs({"a": "1"}, {"a": "1"})["ok"] is True
    diff = fin.compare_runtime_inputs(
        {"a": "1", "b": "MISSING"}, {"a": "2", "b": "MISSING"}
    )
    assert diff["ok"] is False
    assert diff["differing"] == ["a"]
    assert diff["missing_in_clean_source"] == ["b"]


class FakeTrivy:
    def __init__(self, cache: Path, *, mutate_on_scan: bool = False) -> None:
        self.cache = cache
        self.mutate_on_scan = mutate_on_scan

    def __call__(self, argv: list[str], **_: Any) -> Any:
        if "--download-db-only" in argv:
            (self.cache / "db").mkdir(parents=True, exist_ok=True)
            (self.cache / "db/trivy.db").write_bytes(b"db-v1")
            (self.cache / "db/metadata.json").write_text("{}")
        elif self.mutate_on_scan:
            (self.cache / "db/trivy.db").write_bytes(b"db-v2")
        return object()


def _scan_argv() -> list[str]:
    return ia.trivy_scan_argv(
        trivy_bin="trivy",
        cache_dir=Path("/c"),
        image_id="sha256:" + "0" * 64,
        output_path=Path("/o.json"),
        timeout_seconds=300,
    )


def test_db_freeze_proof_ok(tmp_path: Path) -> None:
    rec_runner = fin.DbFreezeRunner(
        inner=FakeTrivy(tmp_path), cache_dir=tmp_path, trivy_bin="trivy"
    )
    rec_runner(["trivy", "--cache-dir", str(tmp_path), "image", "--download-db-only"])
    for _ in range(7):
        rec_runner(_scan_argv())
    rec_runner(["docker", "image", "inspect", "x"])
    proof = rec_runner.proof(fin.hash_db(tmp_path))
    assert proof["ok"] is True
    assert proof["scan_invocations"] == 7
    assert proof["db_download_invocations"] == 1


def test_db_freeze_detects_mutation_double_download_and_online_scan(
    tmp_path: Path,
) -> None:
    mut = fin.DbFreezeRunner(
        inner=FakeTrivy(tmp_path, mutate_on_scan=True),
        cache_dir=tmp_path,
        trivy_bin="trivy",
    )
    mut(["trivy", "image", "--download-db-only"])
    mut(_scan_argv())
    assert mut.proof(fin.hash_db(tmp_path))["snapshot_unchanged_across_scans"] is False

    twice = fin.DbFreezeRunner(
        inner=FakeTrivy(tmp_path), cache_dir=tmp_path, trivy_bin="trivy"
    )
    twice(["trivy", "image", "--download-db-only"])
    twice(["trivy", "image", "--download-db-only"])
    assert twice.proof(fin.hash_db(tmp_path))["ok"] is False

    online = fin.DbFreezeRunner(
        inner=FakeTrivy(tmp_path), cache_dir=tmp_path, trivy_bin="trivy"
    )
    online(["trivy", "image", "--download-db-only"])
    online(["trivy", "image", "--format", "json", "sha256:x"])
    assert online.proof(fin.hash_db(tmp_path))["all_scans_offline_skip_update"] is False


def _gate_summary(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "native_acceptance_history": {"run": "38627426197", "verdict": "PASS"},
        "audit_predecessor": {"run": "38637415562"},
        "approved_runtime_delta": True,
        "runtime_inputs_unchanged": False,
        "verdicts": {"native_acceptance_history": "PASS", "image_security": status},
    }


def test_finalize_findings_are_not_technical() -> None:
    out, technical = fin.finalize_summary(
        _gate_summary(ia.STATUS_FAIL),
        freeze={"ok": True},
        audited_source_sha=SOURCE,
        audit_harness_sha=HEAD,
        source_proof={"ok": True},
    )
    assert technical is False
    assert out["status"] == ia.STATUS_FAIL
    assert out["verdicts"]["native_acceptance_history"] == "NOT_TRANSFERRED"
    assert out["verdicts"]["audit_execution_completeness"] == "PASS"
    assert out["verdicts"]["residual_acceptance"] == "NOT_GRANTED"
    assert out["native_acceptance_history"]["transfers_to_these_images"] is False
    assert out["shas"] == {"audited_source_sha": SOURCE, "audit_harness_sha": HEAD}
    assert "approved_runtime_delta" not in out


@pytest.mark.parametrize(
    ("freeze", "proof", "code"),
    [
        ({"ok": False}, {"ok": True}, "db_snapshot_not_frozen"),
        ({"ok": True}, {"ok": False}, "source_proof_failed"),
    ],
)
def test_finalize_marks_technical(
    freeze: dict[str, Any], proof: dict[str, Any], code: str
) -> None:
    out, technical = fin.finalize_summary(
        _gate_summary(ia.STATUS_FAIL),
        freeze=freeze,
        audited_source_sha=SOURCE,
        audit_harness_sha=HEAD,
        source_proof=proof,
    )
    assert technical is True
    assert out["status"] == ia.STATUS_TECHNICAL
    assert out["error_code"] == code
    assert out["verdicts"]["audit_execution_completeness"] == "FAIL"


# --- isolated smoke -----------------------------------------------------------------


def test_executor_startup_must_fail_closed() -> None:
    assert fin.evaluate_executor_startup(
        1, f"x {fin.EXECUTOR_PREFLIGHT_MARKER}: root"
    ).ok
    assert not fin.evaluate_executor_startup(0, fin.EXECUTOR_PREFLIGHT_MARKER).ok
    assert not fin.evaluate_executor_startup(-1, "timeout").ok
    assert not fin.evaluate_executor_startup(1, "Traceback ImportError").ok


def test_smoke_argv_unprivileged_and_offline() -> None:
    argv = fin.executor_startup_argv("img")
    assert argv[argv.index("--network") + 1] == "none"
    assert "--privileged" not in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert fin.import_argv("img", "x")[3:5] == ["--network", "none"]
    assert fin.shell_argv("img", "true", user="10001:10001")[5:7] == [
        "--user",
        "10001:10001",
    ]


# --- ELF / attribution -----------------------------------------------------------


def build_elf(
    needed: tuple[str, ...] = (),
    *,
    soname: str | None = None,
    imports: tuple[str, ...] = (),
    exports: tuple[str, ...] = (),
    flags_1: int = 0,
) -> bytes:
    dynstr = bytearray(b"\0")

    def add(name: str) -> int:
        offset = len(dynstr)
        dynstr.extend(name.encode() + b"\0")
        return offset

    dyn = b"".join(struct.pack("<qQ", attr.DT_NEEDED, add(n)) for n in needed)
    if soname:
        dyn += struct.pack("<qQ", attr.DT_SONAME, add(soname))
    if flags_1:
        dyn += struct.pack("<qQ", attr.DT_FLAGS_1, flags_1)
    dyn += struct.pack("<qQ", 0, 0)
    syms = b"\0" * 24
    for name in imports:
        syms += struct.pack("<IBBHQQ", add(name), (1 << 4) | 2, 0, 0, 0, 0)
    for name in exports:
        syms += struct.pack("<IBBHQQ", add(name), (1 << 4) | 2, 0, 1, 0x10, 0)
    shstr = b"\0.dynstr\0.dynamic\0.dynsym\0.shstrtab\0"
    blobs = [bytes(dynstr), dyn, syms, shstr]
    offsets, cursor = [], 64
    for blob in blobs:
        offsets.append(cursor)
        cursor += len(blob)
    shoff = cursor
    sections = [struct.pack("<IIQQQQIIQQ", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)]
    specs = [(1, 3, 0, 0), (9, 6, 1, 16), (18, 11, 1, 24), (26, 3, 0, 0)]
    for (name, sh_type, link, entsize), off, blob in zip(
        specs, offsets, blobs, strict=True
    ):
        sections.append(
            struct.pack(
                "<IIQQQQIIQQ", name, sh_type, 0, 0, off, len(blob), link, 0, 1, entsize
            )
        )
    header = b"\x7fELF" + bytes([2, 1, 1]) + b"\0" * 9
    header += struct.pack(
        "<HHIQQQIHHHHHH", 3, 62, 1, 0, 0, shoff, 0, 64, 0, 0, 64, 5, 4
    )
    return header + b"".join(blobs) + b"".join(sections)


def test_parse_elf_dynamic_and_symbols() -> None:
    info = attr.parse_elf(
        build_elf(
            ("libxml2.so.2", "libc.so.6"),
            soname="libavformat.so.61",
            imports=("xmlReadMemory",),
            exports=("avformat_open_input",),
            flags_1=1,
        )
    )
    assert info.error is None
    assert info.needed == ["libxml2.so.2", "libc.so.6"]
    assert info.soname == "libavformat.so.61"
    assert info.imports == {"xmlReadMemory"}
    assert info.exports == {"avformat_open_input"}
    assert info.bind_now is True
    assert attr.parse_elf(b"#!/bin/sh\n" + b"x" * 80).error == "not_elf"


def _tar(files: dict[str, bytes], links: dict[str, str]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, target in links.items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


LIBDIR = "usr/lib/x86_64-linux-gnu"
STATUS = (
    b"Package: libxml2\nStatus: install ok installed\nVersion: 2.9.14-2.1+deb13u3\n\n"
    b"Package: libacl1\nStatus: install ok installed\nVersion: 2.3.2-2+b1\n\n"
    b"Package: gone\nStatus: deinstall ok config-files\nVersion: 1\n"
)


@pytest.fixture
def image_fs() -> attr.ImageFs:
    compat_path = "opt/venv/lib/python3.14/site-packages/yt_dlp/compat/__init__.py"
    files = {
        f"{LIBDIR}/libxml2.so.2.9.14": build_elf(
            ("libc.so.6",),
            soname="libxml2.so.2",
            exports=("xmlReadMemory", "xmlFreeDoc"),
        ),
        f"{LIBDIR}/libavformat.so.61.7.100": build_elf(
            ("libxml2.so.2",), soname="libavformat.so.61", imports=("xmlReadMemory",)
        ),
        f"{LIBDIR}/libacl.so.1.1.2302": build_elf(
            soname="libacl.so.1", exports=("acl_get_file",)
        ),
        f"{LIBDIR}/libc.so.6": build_elf(soname="libc.so.6"),
        "usr/bin/ffmpeg": build_elf(("libavformat.so.61", "libc.so.6")),
        "usr/bin/ls": build_elf(("libacl.so.1",), imports=("acl_get_file",)),
        "usr/local/bin/python3.14": build_elf(("libc.so.6",)),
        "var/lib/dpkg/status": STATUS,
        "var/lib/dpkg/info/libxml2:amd64.list": (
            f"/{LIBDIR}\n/lib/x86_64-linux-gnu/libxml2.so.2\n"
        ).encode(),
        "var/lib/dpkg/info/libacl1:amd64.list": (
            f"/{LIBDIR}/libacl.so.1.1.2302\n"
        ).encode(),
        compat_path: b"import xml.etree.ElementTree\n",
        "opt/venv/lib/python3.14/site-packages/yt_dlp/plain.py": b"x = 1\n",
    }
    links = {
        "lib": "usr/lib",
        f"{LIBDIR}/libxml2.so.2": "libxml2.so.2.9.14",
        f"{LIBDIR}/libavformat.so.61": "libavformat.so.61.7.100",
        f"{LIBDIR}/libacl.so.1": "libacl.so.1.1.2302",
    }
    return attr.scan_tar(_tar(files, links))


def test_scan_tar_ownership_and_resolution(image_fs: attr.ImageFs) -> None:
    assert attr.parse_dpkg_status(image_fs.dpkg_status) == {
        "libxml2": "2.9.14-2.1+deb13u3",
        "libacl1": "2.3.2-2+b1",
    }
    assert (
        attr.resolve_path(image_fs, "/lib/x86_64-linux-gnu/libxml2.so.2")
        == f"/{LIBDIR}/libxml2.so.2.9.14"
    )
    assert attr.owned_elves(image_fs, "libxml2") == [f"/{LIBDIR}/libxml2.so.2.9.14"]
    assert image_fs.ytdlp_etree_users == [
        "/opt/venv/lib/python3.14/site-packages/yt_dlp/compat/__init__.py"
    ]


def test_closure_and_consumers(image_fs: attr.ImageFs) -> None:
    xml = attr.owned_elves(image_fs, "libxml2")
    acl = attr.owned_elves(image_fs, "libacl1")
    chains = attr.closure_chains(image_fs, "/usr/bin/ffmpeg", xml)
    assert chains[xml[0]] == [
        "/usr/bin/ffmpeg",
        f"/{LIBDIR}/libavformat.so.61.7.100",
        xml[0],
    ]
    assert attr.closure_chains(image_fs, "/usr/local/bin/python3.14", acl) == {}
    consumers = attr.direct_consumers(image_fs, acl)
    assert [c["consumer"] for c in consumers] == ["/usr/bin/ls"]
    assert consumers[0]["imported_symbols_sample"] == ["acl_get_file"]


LD_TEXT = (
    "ffprobe_mpd\tcalling init: /usr/lib/x86_64-linux-gnu/libxml2.so.2\n"
    "ffprobe_mpd\tbinding file /usr/lib/x86_64-linux-gnu/libavformat.so.61 [0] to "
    "/usr/lib/x86_64-linux-gnu/libxml2.so.2 [0]: normal symbol "
    "`xmlReadMemory' [LIBXML2_2.6.0]\n"
    "ffprobe_mpd\tbinding file /usr/lib/x86_64-linux-gnu/libavformat.so.61 [0] to "
    "/usr/lib/x86_64-linux-gnu/libxml2.so.2 [0]: normal symbol `xmlCheckVersion'\n"
    "ffprobe_mp4\tcalling init: /usr/lib/x86_64-linux-gnu/libxml2.so.2\n"
    "ffprobe_mp4\tbinding file /usr/lib/x86_64-linux-gnu/libavformat.so.61 [0] to "
    "/usr/lib/x86_64-linux-gnu/libxml2.so.2 [0]: normal symbol `xmlCheckVersion'\n"
)


def test_ld_parse_and_call_specific() -> None:
    ld = attr.parse_ld_lines(LD_TEXT)
    libs = ["/usr/lib/x86_64-linux-gnu/libxml2.so.2.9.14"]
    calls = attr.call_specific(ld, "ffprobe_mpd", "ffprobe_mp4", libs)
    assert [c[2] for c in calls] == ["xmlReadMemory"]
    assert attr.loaded(ld, "ffprobe_mp4", libs) is True
    sections = attr.split_sections("pre\n__LD__\n" + LD_TEXT + "__RC__ x 0\n")
    assert "ffprobe_mpd" in sections["LD"]


def _role(probes: tuple[str, ...] = ()) -> attr.Role:
    return attr.Role(
        "secret-bearing worker",
        ("/usr/bin/ffmpeg",),
        None,
        probes,
        "outside",
        "uid",
        "remote",
    )


def test_classify_confirmed_requires_differential_call() -> None:
    ld = attr.parse_ld_lines(LD_TEXT)
    libs = ["/usr/lib/x86_64-linux-gnu/libxml2.so.2.9.14"]
    row = attr.classify(
        family="libxml2",
        libs=libs,
        role=_role(("ffprobe_mpd",)),
        chains={"x": 1},
        ld=ld,
        maps_libs=[],
        demux_dash=False,
    )
    assert row["verdict"] == "confirmed"
    assert row["confirmed_call_bindings_sample"][0][2] == "xmlReadMemory"


BIND_NOW_LD = (
    "ffprobe_mpd\tcalling init: /usr/lib/x86_64-linux-gnu/libxml2.so.2\n"
    "ffprobe_mp4\tcalling init: /usr/lib/x86_64-linux-gnu/libxml2.so.2\n"
)


def test_classify_libxml2_dash_parse_is_labelled_inferred() -> None:
    ld = attr.parse_ld_lines(BIND_NOW_LD)
    libs = ["/usr/lib/x86_64-linux-gnu/libxml2.so.2.9.14"]
    row = attr.classify(
        family="libxml2",
        libs=libs,
        role=_role(("ffprobe_mpd",)),
        chains={},
        ld=ld,
        maps_libs=[],
        demux_dash=True,
    )
    assert row["verdict"] == "confirmed"
    assert any("inferred call" in e for e in row["evidence"])
    assert "symbol-level call trace (consumer BIND_NOW)" in row["missing_evidence"]
    no_parse = attr.classify(
        family="libxml2",
        libs=libs,
        role=_role(("ffprobe_mpd",)),
        chains={},
        ld=ld,
        maps_libs=[],
        demux_dash=False,
    )
    assert no_parse["verdict"] == "potentially reachable"
    assert "advisory-specific vulnerable function not traced" in row["missing_evidence"]


def test_classify_static_only_is_not_confirmed() -> None:
    row = attr.classify(
        family="libxml2",
        libs=["/l/libxml2.so.2.9.14"],
        role=_role(),
        chains={"/usr/bin/ffmpeg": {"l": ["a"]}},
        ld={},
        maps_libs=[],
        demux_dash=False,
    )
    assert row["verdict"] == "potentially reachable"
    assert (
        "runtime load/call not observed (static linkage only)"
        in row["missing_evidence"]
    )


def test_classify_no_chain_stays_unknown() -> None:
    row = attr.classify(
        family="libacl",
        libs=["/l/libacl.so.1.1.2302"],
        role=_role(),
        chains={},
        ld={},
        maps_libs=["/usr/lib/libc.so.6"],
        demux_dash=False,
    )
    assert row["verdict"] == "UNKNOWN"
    assert any("not proof of unreachability" in m for m in row["missing_evidence"])


def test_classify_libavformat_dash_selection() -> None:
    row = attr.classify(
        family="ffmpeg-libavformat",
        libs=["/l/libavformat.so.61.7.100"],
        role=_role(("ffprobe_mpd",)),
        chains={},
        ld={},
        maps_libs=[],
        demux_dash=True,
    )
    assert row["verdict"] == "confirmed"
    row = attr.classify(
        family="ffmpeg-libavformat",
        libs=["/l/libavformat.so.61.7.100"],
        role=_role(("ffprobe_mpd",)),
        chains={},
        ld={},
        maps_libs=[],
        demux_dash=False,
    )
    assert row["verdict"] == "UNKNOWN"


def test_probe_scripts_use_product_argv_and_controls() -> None:
    script = attr.tool_probe_script()
    assert script.count("-protocol_whitelist file") >= 6
    assert "run ffprobe_mp4" in script and "run ffprobe_mpd" in script
    assert "run mux_mp4" in script and "run mux_mpd" in script
    assert "http" not in script.replace("http://", "")
    etree = attr.etree_probe_script("/opt/venv/bin/python")
    assert "run etree_import" in etree and "run etree_parse" in etree
    assert set(attr.PROBE_CONTROLS) == {"ffprobe_mpd", "mux_mpd", "etree_parse"}


# --- reconciliation ---------------------------------------------------------------


def _summary(image: str, rows: list[dict[str, Any]], db: str) -> dict[str, Any]:
    fix = [r for r in rows if r.get("fixed_version")]
    unf = [r for r in rows if not r.get("fixed_version")]
    return {
        "targets": [
            {
                "name": "api",
                "status": "fail",
                "identity": {"image_id": image, "platform": "linux/amd64"},
            }
        ],
        "high_critical": {"fixable_instances": fix, "unfixed_instances": unf},
        "findings": rows,
        "db": {"metadata_sha256": db, "updated_at": db},
    }


def _r(cid: str, pkg: str, ver: str, fixed: str | None = None) -> dict[str, Any]:
    return {
        "target": "api",
        "id": cid,
        "package": pkg,
        "installed_version": ver,
        "fixed_version": fixed,
        "severity": "HIGH",
    }


def test_reconcile_new_gone_changed() -> None:
    hist = _summary(
        "sha256:old", [_r("CVE-1", "libxml2", "1"), _r("CVE-2", "libacl1", "2")], "db1"
    )
    cur = _summary(
        "sha256:new",
        [_r("CVE-1", "libxml2", "1b"), _r("CVE-3", "openssl", "3", "3.1")],
        "db2",
    )
    out = rec.reconcile(hist, cur)
    hc = out["high_critical"]
    assert hc["ids_new"] == ["CVE-3"] and hc["ids_gone"] == ["CVE-2"]
    assert [c["id"] for c in hc["rows_changed"]] == ["CVE-1"]
    assert [r[1] for r in hc["rows_new"]] == ["CVE-3"]
    assert [r[1] for r in hc["rows_gone"]] == ["CVE-2"]
    assert out["advisory_db"]["same_db_snapshot"] is False
    assert out["fixable_current"]["instances"] == 1
    assert out["historical_owner_approval"]["reapproval_required"] is True
    assert out["historical_owner_approval"]["extended_or_applied"] is False


def test_reconcile_identical_artifacts_do_not_require_reapproval() -> None:
    rows = [_r("CVE-1", "libxml2", "1")]
    out = rec.reconcile(
        _summary("sha256:x", rows, "db"), _summary("sha256:x", rows, "db")
    )
    assert out["all_image_ids_same"] is True
    assert out["advisory_db"]["same_db_snapshot"] is True
    assert out["historical_owner_approval"]["reapproval_required"] is False


# --- workflow invariants ---------------------------------------------------------


def test_workflow_trigger_and_policy_invariants() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch" not in text
    assert "head_commit" not in text and "EXPECTED_SUBJECT" not in text
    assert "      - .github/workflows/sec08-09-final-candidate-image-audit.yml" in text
    assert "github.run_attempt == 1" in text
    assert f"AUDITED_SOURCE_SHA: {SOURCE}" in text
    assert f"AUDIT_PREDECESSOR_SHA: {PREDECESSOR}" in text
    assert "fetch-depth: 3" in text
    assert 'test "$(git rev-parse HEAD^)" = "$AUDIT_PREDECESSOR_SHA"' in text
    assert 'test "$(git rev-parse HEAD^)" = "$AUDITED_SOURCE_SHA"' not in text
    assert 'git archive --format=tar "$AUDITED_SOURCE_SHA"' in text
    assert "2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a" in text
    lowered = text.lower()
    for forbidden in (
        "--ignore-unfixed",
        "--vex",
        "residual_apply",
        "gosu_vex",
        "prune",
        "ssh ",
    ):
        assert forbidden not in lowered
    assert "--severity" not in text
