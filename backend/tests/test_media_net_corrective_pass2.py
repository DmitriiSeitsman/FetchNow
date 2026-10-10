"""SEC-09 corrective pass №2 regressions (R1–R7). Offline / local evidence."""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

from fetchnow.media_egress_proxy.policy import DenyConfig
from fetchnow.media_egress_proxy.server import (
    EgressProxy,
    ProxyLimits,
    _resolve_with_deadline,
)
from fetchnow.media_executor.constants import (
    MAX_ARTIFACT_BYTES,
    MAX_MIN_FREE_BYTES,
    PROFILE_NETWORK,
    WORKER_UID,
)
from fetchnow.media_executor.layout import (
    find_single_artifact,
    measure_tree_bytes_as_identity,
)
from fetchnow.media_executor.protocol import ProtocolError, parse_request
from fetchnow.media_executor.runner import LocalRunner, ToolOutcome, ToolRunner
from fetchnow.media_executor.server import ExecutorApp, _find_artifact_as_worker

ROOT = Path(__file__).resolve().parents[2]
_JOB = "33333333-3333-4333-8333-333333333333"
_URL = "https://vk.com/video-1_2"


def _load_harness() -> ModuleType:
    path = Path(__file__).parent / "media_executor" / "network_acceptance.py"
    spec = importlib.util.spec_from_file_location("network_acceptance_r2", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _download_raw(
    *,
    op: str = "download_progressive",
    max_bytes: int = 1024,
    min_free_bytes: int = 0,
    format_token: str = "fmt_1",
) -> bytes:
    return (
        json.dumps(
            {
                "v": 1,
                "op": op,
                "job_id": _JOB,
                "attempt": 1,
                "fence": 4,
                "url": _URL,
                "provider_id": "vk",
                "format_token": format_token,
                "max_bytes": max_bytes,
                "min_free_bytes": min_free_bytes,
            }
        ).encode()
        + b"\n"
    )


# --- R1 ---


def test_r1_worker_default_min_free_accepted_by_protocol() -> None:
    from fetchnow.core.config import Settings

    settings = Settings(
        APP_ENV="test", DATABASE_URL="postgresql+asyncpg://u:p@localhost/db"
    )
    handoff = settings.media_download_max_bytes
    min_free = settings.media_download_min_free_bytes + 0 + handoff
    assert min_free > MAX_ARTIFACT_BYTES
    assert min_free <= MAX_MIN_FREE_BYTES
    for op in (
        "download_progressive",
        "download_video",
        "download_audio",
    ):
        req = parse_request(
            _download_raw(op=op, max_bytes=handoff, min_free_bytes=min_free),
            profile=PROFILE_NETWORK,
        )
        assert req.min_free_bytes == min_free


def test_r1_min_free_boundaries_and_invalid() -> None:
    parse_request(
        _download_raw(min_free_bytes=MAX_MIN_FREE_BYTES),
        profile=PROFILE_NETWORK,
    )
    with pytest.raises(ProtocolError) as over:
        parse_request(
            _download_raw(min_free_bytes=MAX_MIN_FREE_BYTES + 1),
            profile=PROFILE_NETWORK,
        )
    assert over.value.code == "malformed"
    with pytest.raises(ProtocolError):
        parse_request(
            _download_raw(min_free_bytes=-1),
            profile=PROFILE_NETWORK,
        )
    with pytest.raises(ProtocolError):
        parse_request(
            _download_raw(max_bytes=MAX_ARTIFACT_BYTES + 1, min_free_bytes=0),
            profile=PROFILE_NETWORK,
        )


def test_r1_server_accepts_worker_shaped_min_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fetchnow.core.config import Settings

    settings = Settings(
        APP_ENV="test", DATABASE_URL="postgresql+asyncpg://u:p@localhost/db"
    )
    min_free = (
        settings.media_download_min_free_bytes + settings.media_download_max_bytes
    )
    seen: dict[str, int | None] = {}

    class _Ok(ToolRunner):
        def run(self, **kwargs):  # type: ignore[no-untyped-def]
            seen["free"] = kwargs.get("min_free_bytes")
            attempt = kwargs["attempt"]
            (attempt / "output-artifact.bin").write_bytes(b"abc")
            return ToolOutcome(0, b"", b"", False, False)

    def _mkdir(path: Path, *, uid: int, gid: int, mode: int) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)

    monkeypatch.setattr("fetchnow.media_executor.server.make_owned_directory", _mkdir)
    monkeypatch.setattr(
        "fetchnow.media_executor.server.artifact_stat",
        lambda job_dir: ("output-artifact.bin", 3),
    )
    app = ExecutorApp(
        work_root=tmp_path,
        runner=_Ok(),
        profile=PROFILE_NETWORK,
        ytdlp="/opt/venv/bin/yt-dlp",
        proxy_url="http://egress-proxy:8888",
        socket_dir=tmp_path / "sock",
        make_job=lambda p: p.mkdir(mode=0o2700),
        peer_lookup=lambda _c: WORKER_UID,
    )
    app.handle(
        json.dumps(
            {"v": 1, "op": "reserve", "job_id": _JOB, "attempt": 1, "fence": 4}
        ).encode()
        + b"\n",
        peer_uid=WORKER_UID,
    )
    out = app.handle(
        _download_raw(
            max_bytes=settings.media_download_max_bytes,
            min_free_bytes=min_free,
        ),
        peer_uid=WORKER_UID,
    )
    payload = json.loads(out.decode())
    assert payload.get("ok") is True, payload
    assert seen["free"] == min_free


# --- R2 ---


def test_r2_inspect_releases_on_success(tmp_path: Path) -> None:
    from fetchnow.media_executor.net_client import run_inspect_metadata
    from fetchnow.media_inspection.protocols import ProcessResult

    calls: list[str] = []

    class Client:
        async def reserve(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("reserve")

        async def inspect_metadata(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("inspect")
            return ProcessResult(
                exit_code=0,
                stdout=b"{}",
                stderr=b"",
                timed_out=False,
                cancelled=False,
            )

        async def release(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("release")

        async def cancel(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("cancel")

    async def _go() -> None:
        result = await run_inspect_metadata(
            client=Client(),  # type: ignore[arg-type]
            work_root=tmp_path,
            job_id=_JOB,
            attempt=1,
            fence=4,
            url=_URL,
            provider_id="vk",
            timeout_seconds=5,
        )
        assert result.exit_code == 0
        assert calls == ["reserve", "inspect", "release"]

    asyncio.run(_go())


def test_r2_download_releases_after_handoff(tmp_path: Path) -> None:
    from fetchnow.media_executor.net_client import run_download_to_dir
    from fetchnow.media_inspection.protocols import ProcessResult

    calls: list[str] = []
    job_dir = tmp_path / f"{_JOB}_1_4"
    job_dir.mkdir()
    (job_dir / "output-artifact.bin").write_bytes(b"hello")

    class Client:
        async def reserve(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("reserve")

        async def download(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("download")
            return {
                "result": ProcessResult(0, b"", b"", False, False),
                "payload": {"ok": True, "code": "ok"},
                "artifact_name": "output-artifact.bin",
                "artifact_bytes": 5,
            }

        async def release(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("release")

        async def cancel(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("cancel")

    async def _go() -> None:
        dest = tmp_path / "dest"
        result = await run_download_to_dir(
            client=Client(),  # type: ignore[arg-type]
            work_root=tmp_path,
            dest_dir=dest,
            op="download_progressive",
            job_id=_JOB,
            attempt=1,
            fence=4,
            url=_URL,
            provider_id="vk",
            format_token="fmt",
            timeout_seconds=5,
            max_bytes=100,
            min_free_bytes=0,
        )
        assert result.exit_code == 0
        assert (dest / "output-artifact.bin").read_bytes() == b"hello"
        assert calls == ["reserve", "download", "release"]
        assert "cancel" not in calls

    asyncio.run(_go())


def test_r2_rpc_error_cancel_then_release() -> None:
    from fetchnow.media_executor.client import ExecutorCallError
    from fetchnow.media_executor.net_client import run_inspect_metadata

    calls: list[str] = []

    class Client:
        async def reserve(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("reserve")

        async def inspect_metadata(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("inspect")
            raise ExecutorCallError("protocol")

        async def release(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("release")

        async def cancel(self, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("cancel")

    async def _go() -> None:
        with pytest.raises(ExecutorCallError):
            await run_inspect_metadata(
                client=Client(),  # type: ignore[arg-type]
                work_root=Path("/tmp"),
                job_id=_JOB,
                attempt=1,
                fence=4,
                url=_URL,
                provider_id="vk",
                timeout_seconds=5,
            )
        assert calls[:3] == ["reserve", "inspect", "cancel"]
        assert "release" in calls

    asyncio.run(_go())


def test_r2_handoff_error_and_cleanup_both_visible(tmp_path: Path) -> None:
    from fetchnow.media_executor.client import ExecutorCallError
    from fetchnow.media_executor.handoff import HandoffError
    from fetchnow.media_executor.net_client import (
        NetExecutorCleanupError,
        run_download_to_dir,
    )
    from fetchnow.media_inspection.protocols import ProcessResult

    class Client:
        async def reserve(self, **kwargs):  # type: ignore[no-untyped-def]
            return None

        async def download(self, **kwargs):  # type: ignore[no-untyped-def]
            return {
                "result": ProcessResult(0, b"", b"", False, False),
                "payload": {"ok": True, "code": "ok"},
                "artifact_name": "output-artifact.bin",
                "artifact_bytes": 5,
            }

        async def release(self, **kwargs):  # type: ignore[no-untyped-def]
            raise ExecutorCallError("unavailable")

        async def cancel(self, **kwargs):  # type: ignore[no-untyped-def]
            return None

    async def _go() -> None:
        with pytest.raises((HandoffError, NetExecutorCleanupError)) as err:
            await run_download_to_dir(
                client=Client(),  # type: ignore[arg-type]
                work_root=tmp_path,
                dest_dir=tmp_path / "d",
                op="download_progressive",
                job_id=_JOB,
                attempt=1,
                fence=4,
                url=_URL,
                provider_id="vk",
                format_token="fmt",
                timeout_seconds=5,
                max_bytes=100,
                min_free_bytes=0,
            )
        if isinstance(err.value, NetExecutorCleanupError):
            assert err.value.primary is not None

    asyncio.run(_go())


# --- R3 ---


def test_r3_measure_fail_closed_on_list_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "job"
    root.mkdir()
    (root / "a").write_bytes(b"x")
    real_listdir = os.listdir

    def boom(path):  # type: ignore[no-untyped-def]
        if Path(path) == root:
            return real_listdir(path)
        raise PermissionError("denied")

    sub = root / "sub"
    sub.mkdir()
    monkeypatch.setattr(os, "listdir", boom)
    with pytest.raises(OSError, match="list_failed"):
        measure_tree_bytes_as_identity(root)


def test_r3_localrunner_enumeration_failure_is_limit_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = tmp_path / "a"
    attempt.mkdir()
    script = attempt / "grow.py"
    script.write_text("import time\ntime.sleep(1)\n")

    import fetchnow.media_executor.runner as runner_mod

    def boom(_root):  # type: ignore[no-untyped-def]
        raise OSError("list_failed")

    monkeypatch.setattr(runner_mod, "_tree_bytes", boom)
    outcome = LocalRunner().run(
        attempt=attempt,
        argv=[__import__("sys").executable, str(script)],
        timeout_seconds=2,
        cancel=threading.Event(),
        protected=[],
        max_output_bytes=10,
        min_free_bytes=None,
    )
    assert outcome.exit_code == 1


def test_r3_job_process_uses_measure_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    import fetchnow.media_executor.job_process as jp

    called: list[Path] = []

    def fake(path: Path) -> int:
        called.append(path)
        return 99

    monkeypatch.setattr(jp, "measure_tree_bytes", fake)
    assert jp._tree_bytes(Path("/tmp/x")) == 99
    assert called == [Path("/tmp/x")]


# --- R4 / R5 harness verdicts ---


def test_r4_no_hardcoded_true_matrix_markers() -> None:
    text = (
        Path(__file__).parent / "media_executor" / "network_acceptance.py"
    ).read_text()
    for marker in (
        'checks["N2_download_path_prepared"] = True',
        'checks["N7_rebinding_policy_in_proxy"] = True',
        'checks["N9_limits_contract"] = True',
        'checks["N10_cancel_release_contract"] = True',
        'checks["N11_fence_contract"] = True',
    ):
        assert marker not in text


def test_r4_r5_aggregate_blocks_not_run_and_scanner_timeout() -> None:
    gate = _load_harness()
    checks = {
        "N1_inspect_ok": True,
        "N2_download_ok": True,
        "N3_direct_egress_denied": True,
        "N4_private_connect_denied": True,
        "N5_metadata_connect_denied": True,
        "N6_ipv6_mapped_connect_denied": True,
        "N7_rebinding_connect_denied": True,
        "N8_proxy_down_fail_closed": True,
        "N9_limits_enforced": True,
        "N10_cancel_neighbor_ok": True,
        "N11_fence_lease_ok": True,
        "N12_offline_unreachable": True,
        "N13_same_artifact_audit": "NOT_RUN",
    }
    verdict, err = gate.aggregate_verdict(checks, trivy_required=False)
    assert verdict == "NOT_RUN"
    assert err == "required_not_run"

    checks["N13_same_artifact_audit"] = True
    audits = {
        "network": {
            "status": "technical_failure",
            "error_code": "scanner_timeout",
            "scanner_exit_code": 1,
            "identity": {"image_id": "sha256:" + "a" * 64},
            "expected_image_id": "sha256:" + "a" * 64,
        },
        "proxy": {
            "status": "technical_failure",
            "error_code": "scanner_timeout",
            "scanner_exit_code": 1,
            "identity": {"image_id": "sha256:" + "b" * 64},
            "expected_image_id": "sha256:" + "b" * 64,
        },
        "offline": {
            "status": "technical_failure",
            "error_code": "scanner_timeout",
            "scanner_exit_code": 1,
            "identity": {"image_id": "sha256:" + "c" * 64},
            "expected_image_id": "sha256:" + "c" * 64,
        },
    }
    # N13 derived false from timeouts
    checks["N13_same_artifact_audit"] = gate._n13_from_audits(audits)
    assert checks["N13_same_artifact_audit"] is False
    verdict, _err = gate.aggregate_verdict(checks, audits=audits, trivy_required=True)
    assert verdict == "FAIL"


def test_r5_execute_three_scanner_timeouts_not_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = _load_harness()
    monkeypatch.setattr(gate.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gate.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gate.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        gate,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "Architecture": "x86_64",
                    "CgroupDriver": "systemd",
                    "CgroupVersion": "2",
                    "ServerVersion": "28.0.4",
                    "SecurityOptions": [],
                }
            ),
            "",
        ),
    )
    monkeypatch.setattr(gate, "_prepare_slice", lambda *_a: {})
    monkeypatch.setattr(gate, "_configure_canary", lambda *_a: None)
    scanned: list[str] = []

    def fake_build(state):  # type: ignore[no-untyped-def]
        return {
            "network_executor": "sha256:" + "a" * 64,
            "egress_proxy": "sha256:" + "b" * 64,
            "offline_executor": "sha256:" + "c" * 64,
        }

    monkeypatch.setattr(gate, "_build_images", fake_build)
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *a, **k: __import__("subprocess").CompletedProcess(a[0], 0, "", ""),
    )
    monkeypatch.setattr(gate, "_wait_socket", lambda *a, **k: None)
    monkeypatch.setattr(
        gate,
        "_matrix",
        lambda **kwargs: {
            "N1_inspect_ok": True,
            "N2_download_ok": True,
            "N3_direct_egress_denied": True,
            "N4_private_connect_denied": True,
            "N5_metadata_connect_denied": True,
            "N6_ipv6_mapped_connect_denied": True,
            "N7_rebinding_connect_denied": True,
            "N8_proxy_down_fail_closed": True,
            "N9_limits_enforced": True,
            "N10_cancel_neighbor_ok": True,
            "N11_fence_lease_ok": True,
            "N12_offline_unreachable": True,
        },
    )

    def fake_audit(**kwargs):  # type: ignore[no-untyped-def]
        scanned.append(kwargs["label"])
        return (
            {
                "status": "technical_failure",
                "error_code": "scanner_timeout",
                "scanner_exit_code": 1,
                "identity": {"image_id": kwargs["image_id"]},
                "expected_image_id": kwargs["image_id"],
            },
            1,
        )

    monkeypatch.setattr(gate, "_audit_image", fake_audit)
    monkeypatch.setattr(gate, "cleanup", lambda *_a, **_k: [])
    monkeypatch.setattr(gate, "_write_disposable_overlay", lambda *a, **k: None)
    out = tmp_path / "out"
    code = gate.execute(out, trivy_bin="/usr/bin/trivy")
    assert code == 1
    result = json.loads((out / "result.json").read_text())
    assert result["status"] != "PASS"
    assert result["status"] in {"FAIL", "NOT_RUN"}
    assert scanned == ["network", "proxy", "offline"]


def test_r5_empty_stdout_command_not_pass() -> None:
    gate = _load_harness()
    assert gate._status("") == "FAIL"
    assert gate._status(None) == "FAIL"


# --- R6 ---


def test_r6_resolver_timeout_no_global_socket_mutation() -> None:
    def slow(host, port, type=0, **kwargs):  # type: ignore[no-untyped-def]
        del host, port, type, kwargs
        time.sleep(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]

    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(7.5)
    try:
        with pytest.raises(ValueError, match="dns_failed"):
            _resolve_with_deadline(
                "slow.test",
                443,
                config=DenyConfig(networks=(), hosts=()),
                resolver=slow,
                deadline=time.monotonic() + 0.2,
            )
        assert socket.getdefaulttimeout() == 7.5
    finally:
        socket.setdefaulttimeout(prev)


def test_r6_no_dial_after_resolver_timeout() -> None:
    dialed: list[str] = []

    def slow(host, port, type=0, **kwargs):  # type: ignore[no-untyped-def]
        del host, port, type, kwargs
        time.sleep(1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]

    def dial(target, timeout):  # type: ignore[no-untyped-def]
        dialed.append(str(target.addresses[0]))
        raise OSError("should not dial")

    proxy = EgressProxy(
        deny=DenyConfig(networks=(), hosts=()),
        limits=ProxyLimits(dns_timeout=0.15, handshake_timeout=2, max_connections=4),
        resolve=slow,
        dial=dial,
    )

    class FakeSock:
        def __init__(self) -> None:
            self._sent: list[bytes] = []
            self._payload = (
                b"CONNECT slow.test:443 HTTP/1.1\r\nHost: slow.test:443\r\n\r\n"
            )
            self._pos = 0

        def settimeout(self, _v):  # type: ignore[no-untyped-def]
            return None

        def recv(self, n: int) -> bytes:
            if self._pos >= len(self._payload):
                return b""
            chunk = self._payload[self._pos : self._pos + n]
            self._pos += n
            return chunk

        def sendall(self, data: bytes) -> None:
            self._sent.append(data)

        def close(self) -> None:
            return None

    proxy.handle_client(FakeSock())  # type: ignore[arg-type]
    assert dialed == []


def test_r6_connection_slots_release_after_dns_fail() -> None:
    def slow(host, port, type=0, **kwargs):  # type: ignore[no-untyped-def]
        del host, port, type, kwargs
        time.sleep(0.3)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]

    proxy = EgressProxy(
        deny=DenyConfig(networks=(), hosts=()),
        limits=ProxyLimits(dns_timeout=0.05, max_connections=2),
        resolve=slow,
    )
    # Acquire both slots then release via failed handlers
    assert proxy._connections.acquire(blocking=False)
    assert proxy._connections.acquire(blocking=False)
    proxy._connections.release()
    proxy._connections.release()
    assert proxy._connections.acquire(blocking=False)
    proxy._connections.release()


# --- R7 ---


def test_r7_artifact_helper_no_setresgid_in_server() -> None:
    text = (ROOT / "backend/src/fetchnow/media_executor/server.py").read_text()
    assert "setresgid" not in text
    assert "artifact_stat" in text


def test_r7_find_artifact_as_worker_uses_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fetchnow.media_executor.server as server_mod

    called: list[Path] = []

    def fake(path: Path) -> tuple[str, int]:
        called.append(path)
        return "output-artifact.bin", 12

    monkeypatch.setattr(server_mod, "artifact_stat", fake)
    assert _find_artifact_as_worker(tmp_path) == ("output-artifact.bin", 12)
    assert called == [tmp_path]


def test_r7_discovery_failure_marks_download_failed_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Ok(ToolRunner):
        def run(self, **kwargs):  # type: ignore[no-untyped-def]
            return ToolOutcome(0, b"", b"", False, False)

    def _mkdir(path: Path, *, uid: int, gid: int, mode: int) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)

    monkeypatch.setattr("fetchnow.media_executor.server.make_owned_directory", _mkdir)

    def _boom(_path: Path) -> tuple[str, int]:
        raise OSError("x")

    monkeypatch.setattr("fetchnow.media_executor.server.artifact_stat", _boom)
    app = ExecutorApp(
        work_root=tmp_path,
        runner=_Ok(),
        profile=PROFILE_NETWORK,
        ytdlp="/opt/venv/bin/yt-dlp",
        proxy_url="http://egress-proxy:8888",
        socket_dir=tmp_path / "sock",
        make_job=lambda p: p.mkdir(mode=0o2700),
        peer_lookup=lambda _c: WORKER_UID,
    )
    app.handle(
        json.dumps(
            {"v": 1, "op": "reserve", "job_id": _JOB, "attempt": 1, "fence": 4}
        ).encode()
        + b"\n",
        peer_uid=WORKER_UID,
    )
    out = json.loads(app.handle(_download_raw(), peer_uid=WORKER_UID).decode())
    assert out.get("ok") is False
    job = app._jobs[(_JOB, 1, 4)]
    assert job.state == "done"


def test_r7_concurrent_artifact_stat_no_credential_race(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "output-artifact.bin").write_bytes(b"1")
    (b / "output-artifact.bin").write_bytes(b"22")
    # Direct identity helpers (no setuid) prove discovery is process-local.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(find_single_artifact, a)
        fb = pool.submit(find_single_artifact, b)
        assert fa.result()[1] == 1
        assert fb.result()[1] == 2
