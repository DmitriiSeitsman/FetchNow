"""Security and identity-binding regression tests for media inspection."""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import pytest

from fetchnow.media_inspection.defaults import build_media_inspection_service
from fetchnow.media_inspection.errors import InspectionError, InspectionErrorKind
from fetchnow.media_inspection.process import build_sanitized_env
from fetchnow.media_inspection.protocols import ProcessResult
from fetchnow.media_inspection.ytdlp_parse import parse_ytdlp_json
from fetchnow.resolution.models import ResolutionProvenance
from media_inspection_fixtures import VK_FIXTURE, dumps_fixture
from media_inspection_helpers import (
    FakeRunner,
    make_regular_executable,
    make_temp_root,
    providers,
    resolution,
    settings,
)


def _parse(payload: dict, **kwargs: object):
    return parse_ytdlp_json(
        dumps_fixture(payload),
        expected_provider_id=str(kwargs.get("provider_id", "vk")),
        expected_canonical_url=str(
            kwargs.get("canonical", "https://vk.com/video-123_456239017")
        ),
        expected_media_id=str(kwargs.get("media_id", "-123_456239017")),
        allowed_extractor_keys=frozenset(
            kwargs.get("extractors", frozenset({"vk"}))  # type: ignore[arg-type]
        ),
        allowed_hostnames=frozenset(
            kwargs.get(
                "hosts",
                frozenset(
                    {
                        "vk.com",
                        "www.vk.com",
                        "m.vk.com",
                        "vkvideo.ru",
                        "www.vkvideo.ru",
                        "m.vkvideo.ru",
                    }
                ),
            )  # type: ignore[arg-type]
        ),
        max_height=2160,
        max_width=3840,
        max_bytes=10**9,
        max_duration=3600,
        tool_version="2026.7.4",
    )


@pytest.mark.asyncio
async def test_forged_resolution_reaches_tool_false(tmp_path: Path) -> None:
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    with pytest.raises(InspectionError) as exc:
        await svc.inspect(
            resolution(
                provider_id="vk",
                canonical="https://vk.com/video-123_456239017",
                hostname="vk.com",
                path="/video-123_456239017",
                validated_provider_id="rutube",
                validated_hostname="rutube.ru",
                validated_canonical="https://rutube.ru/video/abc123def456/",
                validated_path="/video/abc123def456/",
            )
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH
    assert runner.calls == []


@pytest.mark.asyncio
async def test_validated_snapshot_mismatch_accepted_false(tmp_path: Path) -> None:
    runner = FakeRunner()
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    with pytest.raises(InspectionError):
        await svc.inspect(
            resolution(
                provider_id="vk",
                canonical="https://vk.com/video-999_1",
                hostname="vk.com",
                path="/video-999_1",
                validated_canonical="https://vk.com/video-123_456239017",
                validated_path="/video-123_456239017",
            )
        )
    assert runner.calls == []


def test_same_host_different_media_accepted_false() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["webpage_url"] = "https://vk.com/video-999_888"
    payload["original_url"] = "https://vk.com/video-999_888"
    payload["id"] = "-999_888"
    with pytest.raises(InspectionError) as exc:
        _parse(payload)
    assert exc.value.kind is InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH


def test_cross_alias_same_identity_accepted_true() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["webpage_url"] = "https://m.vk.com/video-123_456239017"
    payload["original_url"] = "https://vkvideo.ru/video-123_456239017"
    draft = _parse(payload)
    assert draft.media_id == "-123_456239017"
    assert draft.canonical_provider_url == "https://vk.com/video-123_456239017"


def test_clip_webpage_url_binds_to_video_canonical_target() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["webpage_url"] = "https://vk.ru/clip-123_456239017"
    payload["original_url"] = "https://m.vk.ru/clip-123_456239017"
    draft = _parse(
        payload,
        canonical="https://vk.ru/video-123_456239017",
        media_id="-123_456239017",
        hosts=frozenset(
            {
                "vk.com",
                "www.vk.com",
                "m.vk.com",
                "vk.ru",
                "www.vk.ru",
                "m.vk.ru",
                "vkvideo.ru",
                "www.vkvideo.ru",
                "m.vkvideo.ru",
            }
        ),
    )
    assert draft.media_id == "-123_456239017"
    assert draft.canonical_provider_url == "https://vk.ru/video-123_456239017"


def test_missing_authoritative_identity_accepted_false() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    del payload["webpage_url"]
    del payload["original_url"]
    with pytest.raises(InspectionError) as exc:
        _parse(payload)
    assert exc.value.kind is InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH


def test_conflicting_identity_fields_accepted_false() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    payload["webpage_url"] = "https://vk.com/video-123_456239017"
    payload["original_url"] = "https://vk.com/video-999_1"
    with pytest.raises(InspectionError) as exc:
        _parse(payload)
    assert exc.value.kind is InspectionErrorKind.INSPECTION_PROVIDER_MISMATCH


def test_extractor_can_replace_canonical_false() -> None:
    payload = copy.deepcopy(VK_FIXTURE)
    # Even if payload advertises another host alias, draft canonical stays trusted.
    payload["webpage_url"] = "https://m.vk.com/video-123_456239017"
    draft = _parse(
        payload,
        canonical="https://vk.com/video-123_456239017",
    )
    assert draft.canonical_provider_url == "https://vk.com/video-123_456239017"


def test_video_ext_and_embed_paths_rejected() -> None:
    for bad in (
        "https://vk.com/video_ext.php?oid=-1&id=2",
        "https://vk.com/video_ext?oid=-1",
        "https://rutube.ru/play/embed/abc123def456/",
        "https://rutube.ru/video/abc123def456.m3u8",
    ):
        payload = copy.deepcopy(VK_FIXTURE)
        payload["webpage_url"] = bad
        payload["original_url"] = bad
        with pytest.raises(InspectionError):
            _parse(payload)


@pytest.mark.asyncio
async def test_wrapper_url_reaches_tool_directly_false(tmp_path: Path) -> None:
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    await svc.inspect(
        resolution(
            provider_id="vk",
            canonical="https://vk.com/video-123_456239017",
            hostname="vk.com",
            path="/video-123_456239017",
            provenance=ResolutionProvenance.WRAPPER_RESOLVED,
        )
    )
    assert all("yandex" not in c["argv"][-1] for c in runner.calls)


@pytest.mark.asyncio
async def test_credentials_rejected(tmp_path: Path) -> None:
    runner = FakeRunner()
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    with pytest.raises(InspectionError) as exc:
        await svc.inspect(
            resolution(
                provider_id="vk",
                canonical="https://user:pass@vk.com/video-1_2",
                hostname="vk.com",
                path="/video-1_2",
            )
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_POLICY_REJECTED
    assert runner.calls == []


@pytest.mark.asyncio
async def test_nondefault_port_rejected(tmp_path: Path) -> None:
    runner = FakeRunner()
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    with pytest.raises(InspectionError) as exc:
        await svc.inspect(
            resolution(
                provider_id="vk",
                canonical="https://vk.com:8443/video-1_2",
                hostname="vk.com",
                path="/video-1_2",
            )
        )
    assert exc.value.kind is InspectionErrorKind.INSPECTION_POLICY_REJECTED
    assert runner.calls == []


@pytest.mark.asyncio
async def test_lookalike_host_never_reaches_tool(tmp_path: Path) -> None:
    runner = FakeRunner()
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    with pytest.raises(InspectionError):
        await svc.inspect(
            resolution(
                provider_id="vk",
                canonical="https://vk.com.evil.example/video-1_2",
                hostname="vk.com.evil.example",
                path="/video-1_2",
            )
        )
    assert runner.calls == []


@pytest.mark.asyncio
async def test_query_and_fragment_not_in_tool_url_or_repr(tmp_path: Path) -> None:
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    meta = await svc.inspect(
        resolution(
            provider_id="vk",
            canonical="https://vk.com/video-123_456239017",
            hostname="vk.com",
            path="/video-123_456239017",
            query="access_token=SECRET",
        )
    )
    tool_url = runner.calls[0]["argv"][-1]
    assert "?" not in tool_url
    assert "#" not in tool_url
    assert "SECRET" not in tool_url
    assert "SECRET" not in repr(meta)


@pytest.mark.asyncio
async def test_stderr_and_json_absent_from_logging_capture(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"stderr-secret-token",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    with caplog.at_level(logging.INFO):
        meta = await svc.inspect(
            resolution(
                provider_id="vk",
                canonical="https://vk.com/video-123_456239017",
                hostname="vk.com",
                path="/video-123_456239017",
            )
        )
    joined = " ".join(r.message for r in caplog.records)
    assert "stderr-secret-token" not in joined
    assert "PLACEHOLDER" not in joined
    assert "example.invalid" not in joined
    assert "PLACEHOLDER" not in repr(meta)


def test_subprocess_env_contains_no_secrets() -> None:
    env = build_sanitized_env(home_dir="/tmp/h", tmp_dir="/tmp/t")
    for key in ("DATABASE_URL", "POSTGRES_PASSWORD", "HTTP_PROXY", "SECRET"):
        assert key not in env


@pytest.mark.asyncio
async def test_error_messages_catalog_only_no_urls(tmp_path: Path) -> None:
    runner = FakeRunner(
        ProcessResult(
            exit_code=2,
            stdout=b"",
            stderr=b"ERROR: https://vk.com/video-1_2 failed with token=abc",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    with pytest.raises(InspectionError) as exc:
        await svc.inspect(
            resolution(
                provider_id="vk",
                canonical="https://vk.com/video-123_456239017",
                hostname="vk.com",
                path="/video-123_456239017",
            )
        )
    text = str(exc.value) + repr(exc.value)
    assert "vk.com" not in text
    assert "token=abc" not in text
    assert "https://" not in text


_COHERENT_VK = {
    "provider_id": "vk",
    "canonical": "https://vk.com/video-123_456239017",
    "hostname": "vk.com",
    "path": "/video-123_456239017",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutated",
    [
        # canonical hostname vs structured hostname
        resolution(
            provider_id="vk",
            canonical="https://m.vk.com/video-123_456239017",
            hostname="vk.com",
            path="/video-123_456239017",
            validated_canonical="https://m.vk.com/video-123_456239017",
        ),
        # canonical path vs structured path
        resolution(
            provider_id="vk",
            canonical="https://vk.com/video-999_1",
            hostname="vk.com",
            path="/video-123_456239017",
            validated_canonical="https://vk.com/video-999_1",
            public_canonical="https://vk.com/video-999_1",
        ),
        # structured hostname
        resolution(**{**_COHERENT_VK, "validated_hostname": "m.vk.com"}),
        # structured path
        resolution(**{**_COHERENT_VK, "validated_path": "/video-999_1"}),
        # scheme
        resolution(**{**_COHERENT_VK, "scheme": "http"}),
        # original_scheme
        resolution(**{**_COHERENT_VK, "original_scheme": "http"}),
        # port (default https port must be None after normalize)
        resolution(**{**_COHERENT_VK, "port": 443}),
        # provider_id
        resolution(**{**_COHERENT_VK, "validated_provider_id": "rutube"}),
        # public canonical query (no strip-and-accept)
        resolution(
            **{
                **_COHERENT_VK,
                "public_canonical": "https://vk.com/video-123_456239017?leak=1",
            }
        ),
        # public canonical fragment
        resolution(
            **{
                **_COHERENT_VK,
                "public_canonical": "https://vk.com/video-123_456239017#frag",
            }
        ),
    ],
)
async def test_snapshot_coherence_mismatch_never_reaches_tool(
    tmp_path: Path,
    mutated,
) -> None:
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    with pytest.raises(InspectionError):
        await svc.inspect(mutated)
    assert runner.calls == []


@pytest.mark.asyncio
async def test_coherent_snapshot_reaches_tool_true(tmp_path: Path) -> None:
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    await svc.inspect(resolution(**_COHERENT_VK))
    assert len(runner.calls) == 1
    assert runner.calls[0]["argv"][-1] == "https://vk.com/video-123_456239017"


@pytest.mark.asyncio
async def test_validated_query_never_reaches_tool_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "validated-query-secret-token-9f3a"
    runner = FakeRunner(
        ProcessResult(
            exit_code=0,
            stdout=dumps_fixture(VK_FIXTURE),
            stderr=b"",
            timed_out=False,
            cancelled=False,
        )
    )
    s = settings(
        MEDIA_INSPECTION_YTDLP_PATH=make_regular_executable(tmp_path),
        MEDIA_INSPECTION_TEMP_ROOT=make_temp_root(tmp_path),
    )
    svc = build_media_inspection_service(s, providers(s), runner=runner)
    with caplog.at_level(logging.DEBUG):
        await svc.inspect(resolution(**_COHERENT_VK, query=f"token={secret}"))
    tool_url = runner.calls[0]["argv"][-1]
    assert secret not in tool_url
    assert "?" not in tool_url
    joined = " ".join(r.message for r in caplog.records) + tool_url
    assert secret not in joined
    assert f"token={secret}" not in joined
