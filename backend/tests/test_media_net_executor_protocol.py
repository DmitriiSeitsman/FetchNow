"""SEC-09 network protocol / argv regressions. Not Compose isolation proof."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fetchnow.media_executor.argv import network_download_argv, network_inspect_argv
from fetchnow.media_executor.constants import (
    PROFILE_NETWORK,
    WORKER_UID,
)
from fetchnow.media_executor.protocol import ProtocolError, parse_request
from fetchnow.media_executor.runner import ToolOutcome, ToolRunner
from fetchnow.media_executor.server import ExecutorApp

_JOB = "11111111-1111-4111-8111-111111111111"
_URL = "https://vk.com/video-1_2"


def _net_request(
    op: str,
    *,
    url: str = _URL,
    provider_id: str = "vk",
    format_token: str | None = "fmt_1",
    extra: dict[str, object] | None = None,
) -> bytes:
    fields: dict[str, object] = {
        "v": 1,
        "op": op,
        "job_id": _JOB,
        "attempt": 1,
        "fence": 4,
        "url": url,
        "provider_id": provider_id,
    }
    if format_token is not None:
        fields["format_token"] = format_token
    if op.startswith("download_"):
        fields.setdefault("max_bytes", 1024)
        fields.setdefault("min_free_bytes", 0)
    if extra:
        fields.update(extra)
    return json.dumps(fields).encode("utf-8") + b"\n"


def test_offline_still_rejects_network_ops() -> None:
    with pytest.raises(ProtocolError) as err:
        parse_request(_net_request("inspect_metadata", format_token=None))
    assert err.value.code == "network_not_in_sec08"


def test_network_rejects_offline_ops() -> None:
    raw = json.dumps(
        {
            "v": 1,
            "op": "mux_copy",
            "job_id": _JOB,
            "attempt": 1,
            "fence": 4,
            "container": "mp4",
        }
    ).encode() + b"\n"
    with pytest.raises(ProtocolError) as err:
        parse_request(raw, profile=PROFILE_NETWORK)
    assert err.value.code == "offline_not_in_sec09"


def test_network_rejects_argv_proxy_and_bad_url() -> None:
    with pytest.raises(ProtocolError) as argv:
        parse_request(
            _net_request("inspect_metadata", format_token=None, extra={"argv": ["sh"]}),
            profile=PROFILE_NETWORK,
        )
    assert argv.value.code == "malformed"
    with pytest.raises(ProtocolError) as proxy:
        parse_request(
            _net_request(
                "inspect_metadata",
                format_token=None,
                extra={"proxy": "http://evil"},
            ),
            profile=PROFILE_NETWORK,
        )
    assert proxy.value.code == "malformed"
    with pytest.raises(ProtocolError) as bad_url:
        parse_request(
            _net_request(
                "inspect_metadata",
                url="https://user:pass@vk.com/video",
                format_token=None,
            ),
            profile=PROFILE_NETWORK,
        )
    assert bad_url.value.code == "malformed"
    with pytest.raises(ProtocolError) as query:
        parse_request(
            _net_request(
                "inspect_metadata",
                url="https://vk.com/video?x=1",
                format_token=None,
            ),
            profile=PROFILE_NETWORK,
        )
    assert query.value.code == "malformed"


def test_network_argv_sets_proxy_and_forbids_external_downloader() -> None:
    inspect = network_inspect_argv(
        executable="/opt/venv/bin/yt-dlp",
        url=_URL,
        provider_id="vk",
        proxy_url="http://egress-proxy:8888",
        socket_timeout=10,
        cache_dir=Path("/tmp/cache"),
    )
    assert "--proxy" in inspect
    assert inspect[inspect.index("--proxy") + 1] == "http://egress-proxy:8888"
    assert "--external-downloader" not in inspect
    assert "--ffmpeg-location" not in inspect
    download = network_download_argv(
        executable="/opt/venv/bin/yt-dlp",
        url=_URL,
        provider_id="vk",
        format_token="fmt_1",
        proxy_url="http://egress-proxy:8888",
        socket_timeout=30,
        cache_dir=Path("/tmp/cache"),
        output_template="output-artifact.%(ext)s",
        max_filesize_bytes=1024,
    )
    assert "--proxy" in download
    assert "--max-filesize" in download


def test_network_inspect_success_path(tmp_path: Path) -> None:
    class _Ok(ToolRunner):
        def run(
            self,
            *,
            attempt: Path,
            argv: list[str],
            timeout_seconds: float,
            cancel: object,
            protected: list[str],
            max_output_bytes: int | None = None,
            min_free_bytes: int | None = None,
        ) -> ToolOutcome:
            del attempt, timeout_seconds, cancel, protected
            assert "--proxy" in argv
            assert "http://egress-proxy:8888" in argv
            return ToolOutcome(0, b'{"id":"x"}', b"", False, False)

    app = ExecutorApp(
        work_root=tmp_path,
        runner=_Ok(),
        profile=PROFILE_NETWORK,
        ytdlp="/opt/venv/bin/yt-dlp",
        proxy_url="http://egress-proxy:8888",
        socket_dir=tmp_path / "sock",
        peer_lookup=lambda _c: WORKER_UID,
    )
    assert b"reserved" in app.handle(
        json.dumps(
            {"v": 1, "op": "reserve", "job_id": _JOB, "attempt": 1, "fence": 4}
        ).encode()
        + b"\n",
        peer_uid=WORKER_UID,
    )
    body = app.handle(
        _net_request("inspect_metadata", format_token=None),
        peer_uid=WORKER_UID,
    )
    assert b'"ok":true' in body


_DB = "postgresql+asyncpg://fetchnow:fetchnow@localhost:5432/fetchnow"


def test_flag_defaults_keep_worker_local_path() -> None:
    from fetchnow.core.config import Settings

    settings = Settings(APP_ENV="test", DATABASE_URL=_DB)
    assert settings.media_net_executor_enabled is False
    assert settings.media_executor_enabled is False


def test_net_flag_requires_absolute_socket_and_work_root() -> None:
    from fetchnow.core.config import Settings

    with pytest.raises(ValueError, match="MEDIA_NET_EXECUTOR_SOCKET"):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_NET_EXECUTOR_ENABLED=True,
        )
    with pytest.raises(ValueError, match="MEDIA_NET_EXECUTOR_WORK_ROOT"):
        Settings(
            APP_ENV="test",
            DATABASE_URL=_DB,
            MEDIA_NET_EXECUTOR_ENABLED=True,
            MEDIA_NET_EXECUTOR_SOCKET="/run/fetchnow-media-net/ctrl/worker.sock",
        )
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=_DB,
        MEDIA_NET_EXECUTOR_ENABLED=True,
        MEDIA_NET_EXECUTOR_SOCKET="/run/fetchnow-media-net/ctrl/worker.sock",
        MEDIA_NET_EXECUTOR_WORK_ROOT="/var/lib/fetchnow/media-net",
    )
    assert settings.media_net_executor_enabled is True


def test_download_requires_max_bytes() -> None:
    raw = json.dumps(
        {
            "v": 1,
            "op": "download_progressive",
            "job_id": _JOB,
            "attempt": 1,
            "fence": 4,
            "url": _URL,
            "provider_id": "vk",
            "format_token": "fmt_1",
        }
    ).encode() + b"\n"
    with pytest.raises(ProtocolError) as err:
        parse_request(raw, profile=PROFILE_NETWORK)
    assert err.value.code == "malformed"


def test_download_max_bytes_clamped_to_server_ceiling(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    class _Capture(ToolRunner):
        def run(
            self,
            *,
            attempt: Path,
            argv: list[str],
            timeout_seconds: float,
            cancel: object,
            protected: list[str],
            max_output_bytes: int | None = None,
            min_free_bytes: int | None = None,
        ) -> ToolOutcome:
            del attempt, timeout_seconds, cancel, protected
            seen.append(argv)
            assert max_output_bytes == 100
            assert min_free_bytes == 0
            out = Path(argv[argv.index("-o") + 1].replace("%(ext)s", "bin"))
            out.write_bytes(b"x")
            # argv uses output template; create matching artifact for server
            return ToolOutcome(0, b"", b"", False, False)

    # Build a runner that also drops artifact the server expects.
    class _Ok(_Capture):
        def run(self, **kwargs):  # type: ignore[no-untyped-def]
            attempt = kwargs["attempt"]
            (attempt / "output-artifact.bin").write_bytes(b"x" * 10)
            return super().run(**kwargs)

    app = ExecutorApp(
        work_root=tmp_path,
        runner=_Ok(),
        profile=PROFILE_NETWORK,
        ytdlp="/opt/venv/bin/yt-dlp",
        proxy_url="http://egress-proxy:8888",
        max_filesize_bytes=100,
        socket_dir=tmp_path / "sock",
        peer_lookup=lambda _c: WORKER_UID,
    )
    assert b"reserved" in app.handle(
        json.dumps(
            {"v": 1, "op": "reserve", "job_id": _JOB, "attempt": 1, "fence": 4}
        ).encode()
        + b"\n",
        peer_uid=WORKER_UID,
    )
    body = app.handle(
        _net_request(
            "download_progressive",
            extra={"max_bytes": 10_000, "min_free_bytes": 0},
        ),
        peer_uid=WORKER_UID,
    )
    assert b'"ok":true' in body or b'"ok": false' in body or b'"ok":false' in body
    assert seen
    assert "--max-filesize" in seen[0]
    assert seen[0][seen[0].index("--max-filesize") + 1] == "100"
