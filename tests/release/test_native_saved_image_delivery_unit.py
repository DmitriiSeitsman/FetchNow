"""Offline regressions for the native saved-image delivery harness/workflow.

These are not native proof. They lock the pure decision helpers, the exact
production messages the native refusal/positive checks depend on, and the
one-shot workflow contract (subject marker, two native jobs, bounded steps).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = ROOT / "scripts/native_saved_image_delivery_acceptance.py"
WORKFLOW = ROOT / ".github/workflows/sec08-09-saved-image-delivery.yml"
EXPECTED_SUBJECT = (
    "test(security): SEC-08/09 native saved-image delivery acceptance "
    "[native-once-20261024-imgbndl]"
)
ID_A = "sha256:" + "a" * 64
ID_B = "sha256:" + "b" * 64
ID_C = "sha256:" + "c" * 64


def _load_harness():
    spec = importlib.util.spec_from_file_location("saved_image_harness", HARNESS_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["saved_image_harness"] = module
    spec.loader.exec_module(module)
    return module


h = _load_harness()


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _inv_line(image_id: str, repo: str = "<none>", tag: str = "<none>") -> str:
    return json.dumps({"ID": image_id, "Repository": repo, "Tag": tag})


def test_parse_inventory_ignores_untagged_and_rejects_conflicts() -> None:
    inv = h.parse_inventory(
        "\n".join(
            [
                _inv_line(ID_A, "fetchnow-api", "r1"),
                _inv_line(ID_B),
                _inv_line(ID_C, "postgres", "<none>"),
            ]
        )
    )
    assert inv == {"tags": {"fetchnow-api:r1": ID_A}, "ids": sorted([ID_A, ID_B, ID_C])}
    with pytest.raises(h.AcceptanceError):
        h.parse_inventory(_inv_line(ID_A, "x", "1") + "\n" + _inv_line(ID_B, "x", "1"))
    with pytest.raises(h.AcceptanceError):
        h.parse_inventory(_inv_line("sha256:short"))


def test_inventory_delta_reports_new_removed_and_retargeted() -> None:
    before = {"tags": {"a:1": ID_A, "b:1": ID_B}, "ids": [ID_A, ID_B]}
    after = {"tags": {"a:1": ID_C, "c:1": ID_C}, "ids": [ID_A, ID_C]}
    delta = h.inventory_delta(before, after)
    assert delta == {
        "new_ids": [ID_C],
        "removed_ids": [ID_B],
        "new_tags": {"c:1": ID_C},
        "removed_tags": ["b:1"],
        "retargeted_tags": ["a:1"],
    }
    assert h.inventory_delta(before, before) == {
        "new_ids": [],
        "removed_ids": [],
        "new_tags": {},
        "removed_tags": [],
        "retargeted_tags": [],
    }


def test_event_policy_allows_only_load_and_tag_during_import() -> None:
    lines = "\n".join(
        json.dumps(e)
        for e in (
            {"Type": "image", "Action": "load", "Actor": {"ID": ID_A}, "timeNano": 1},
            {
                "Type": "image",
                "Action": "tag",
                "Actor": {"ID": ID_A, "Attributes": {"name": "x:1"}},
            },
            {"Type": "image", "Action": "pull", "Actor": {"ID": "postgres:16"}},
            {"Type": "container", "Action": "create", "Actor": {"ID": "c1"}},
            {"Type": "builder", "Action": "prune", "Actor": {"ID": ""}},
        )
    )
    events = h.parse_events(lines)
    assert [e["action"] for e in events] == ["load", "tag", "pull", "create", "prune"]
    bad = h.event_violations(events, allowed_image_actions=h.IMPORT_IMAGE_ACTIONS)
    assert [(e["type"], e["action"]) for e in bad] == [
        ("image", "pull"),
        ("container", "create"),
        ("builder", "prune"),
    ]
    assert len(h.event_violations(events, allowed_image_actions=frozenset())) == 5
    assert h.event_violations([], allowed_image_actions=frozenset()) == []


def test_build_cache_count_parser() -> None:
    rows = "\n".join(
        json.dumps(r)
        for r in (
            {"Type": "Images", "TotalCount": "3"},
            {"Type": "Build Cache", "TotalCount": "0"},
        )
    )
    assert h.parse_build_cache_count(rows) == 0
    with pytest.raises(h.AcceptanceError):
        h.parse_build_cache_count(json.dumps({"Type": "Images", "TotalCount": "1"}))


def test_link_copy_isolates_mutated_file_from_original(tmp_path: Path) -> None:
    src = tmp_path / "bundle"
    src.mkdir()
    (src / "api.tar").write_bytes(b"0123456789")
    (src / "web.tar").write_bytes(b"web")
    (src / "image-bundle.json").write_text("{}")
    original = h.bundle_file_hashes(src)

    corrupt = tmp_path / "corrupt"
    h.link_copy(src, corrupt, real_copy=("api.tar",))
    assert os.stat(corrupt / "web.tar").st_ino == os.stat(src / "web.tar").st_ino
    assert os.stat(corrupt / "api.tar").st_ino != os.stat(src / "api.tar").st_ino
    h.flip_byte(corrupt / "api.tar", 5)
    assert (corrupt / "api.tar").read_bytes() != (src / "api.tar").read_bytes()

    missing = tmp_path / "missing"
    h.link_copy(src, missing, omit=("web.tar",))
    assert sorted(p.name for p in missing.iterdir()) == ["api.tar", "image-bundle.json"]
    assert h.bundle_file_hashes(src) == original

    with pytest.raises(FileExistsError):
        h.link_copy(src, missing)
    (src / "link").symlink_to(src / "web.tar")
    with pytest.raises(h.AcceptanceError):
        h.link_copy(src, tmp_path / "symlinked")


def test_flip_byte_refuses_offset_outside_file(tmp_path: Path) -> None:
    path = tmp_path / "x.tar"
    path.write_bytes(b"ab")
    with pytest.raises(h.AcceptanceError):
        h.flip_byte(path, 9)


def test_directory_digest_detects_change_and_does_not_follow_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release"
    (root / "source").mkdir(parents=True)
    (root / "release.json").write_text("{}")
    (root / "source" / "a").write_text("a")
    (root / "ln").symlink_to("/etc/passwd")
    first = h.directory_digest(root)
    assert first["ln"] == "symlink:/etc/passwd"
    assert first["source"] == "dir"
    (root / "source" / "a").write_text("b")
    assert h.directory_digest(root) != first
    assert h.directory_digest(tmp_path / "absent") == {}


def test_compare_builder_importer_flags_each_identity_field() -> None:
    builder = {
        "api": {"image_id": ID_A, "platform": "linux/amd64", "revision_label": "r"},
        "postgres": {
            "image_id": ID_B,
            "platform": "linux/amd64",
            "revision_label": None,
        },
    }
    assert h.compare_builder_importer(builder, json.loads(json.dumps(builder))) == []
    importer = json.loads(json.dumps(builder))
    importer["api"]["image_id"] = ID_C
    importer["postgres"]["platform"] = "linux/arm64"
    assert h.compare_builder_importer(builder, importer) == [
        "api:image_id",
        "postgres:platform",
    ]
    assert "service set differs" in h.compare_builder_importer(
        builder, {"api": builder["api"]}
    )


def test_privacy_scan_finds_secret_patterns(tmp_path: Path) -> None:
    (tmp_path / "ok.json").write_text('{"a": 1}')
    (tmp_path / "redacted.txt").write_text("POSTGRES_PASSWORD=<redacted>\n")
    assert h.privacy_hits(tmp_path, ("A" * 32,)) == []
    (tmp_path / "bad.txt").write_text("POSTGRES_PASSWORD=" + "x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "leak.txt").write_text("A" * 32)
    assert h.privacy_hits(tmp_path, ("A" * 32,)) == ["bad.txt", "sub/leak.txt"]


def test_native_phases_are_not_run_without_explicit_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(h.EXECUTE_ENV, raising=False)
    evidence = tmp_path / "evidence"
    rc = h.main(
        [
            "builder",
            "--tooling-sha",
            "0" * 40,
            "--evidence-dir",
            str(evidence),
            "--bundle-dir",
            str(tmp_path / "bundle"),
            "--work-parent",
            str(tmp_path),
        ]
    )
    assert rc == 2
    assert not evidence.exists()


def test_evidence_records_primary_error_and_not_run_cases(tmp_path: Path) -> None:
    ev = h.Evidence(tmp_path / "e", "importer")
    ev.case("first", True)
    with pytest.raises(h.AcceptanceError):
        ev.case("second", False, why="x")
    ev.primary_error = "AcceptanceError: case failed: second"
    ev.finish("FAIL", ("first", "second", "third"))
    result = json.loads((tmp_path / "e" / "result.json").read_text())
    assert result["status"] == "FAIL"
    assert result["case_counts"] == {"PASS": 1, "FAIL": 1, "NOT_RUN": 1}
    assert result["security_approval_applied"] is False
    assert result["production_installation"] == "NOT_PERFORMED"
    with pytest.raises(FileExistsError):
        h.Evidence(tmp_path / "e", "importer")


def test_postgres_candidate_satisfies_production_bundle_contract() -> None:
    from fetchnow_release.image_bundle import _postgres_identity

    pinned = _postgres_identity(dict(h.POSTGRES_CANDIDATE))
    assert pinned["config_image_id"].startswith("sha256:6a2e324e")
    assert pinned["registry_index_digest"].startswith("sha256:f4610795")
    assert pinned["platform_manifest_digest_linux_amd64"].startswith("sha256:cbf42f0e")


def test_expected_production_messages_exist_verbatim() -> None:
    sources = "".join(
        (ROOT / "scripts/fetchnow_release" / name).read_text(encoding="utf-8")
        for name in ("prepare.py", "image_bundle.py")
    )
    for name in dir(h):
        if name.startswith(("MARK_", "REASON_")):
            assert getattr(h, name) in sources, name


def test_harness_pins_match_workflow_pins() -> None:
    text = _workflow()
    for value in (h.PRODUCT_SHA, h.PRODUCT_TREE, h.TOOLING_PARENT):
        assert value in text
    assert h.RELEASE_MODE == "media"
    assert h.OVERLAY == "compose.staging.yaml"


def test_workflow_is_push_only_exact_subject_attempt_one() -> None:
    text = _workflow()
    assert "workflow_dispatch" not in text
    assert (
        "on:\n  push:\n    branches: [codex/security-sec-09-network-isolation]" in text
    )
    assert EXPECTED_SUBJECT in text
    assert "message.splitlines()[0]" in text
    assert 'head.get("id") == os.environ["GITHUB_SHA"]' in text
    assert "github.run_attempt == 1" in text
    for other in (ROOT / ".github/workflows").glob("*.yml"):
        if other != WORKFLOW:
            assert "native-once-20261024-imgbndl" not in other.read_text(
                encoding="utf-8"
            )


def test_workflow_two_native_jobs_and_publication_gate() -> None:
    text = _workflow()
    assert "  builder:\n    needs: authorize\n" in text
    assert "  importer:\n    needs: [authorize, builder]\n" in text
    assert "needs.builder.result == 'success'" in text
    assert "needs.builder.outputs.published == 'true'" in text
    assert text.count("runs-on: ubuntu-24.04") == 3
    builder = text.split("  builder:\n", 1)[1].split("  importer:\n", 1)[0]
    assert builder.index("Publish complete bundle") < builder.index(
        "Confirm bundle publication"
    )
    assert builder.index("Confirm bundle publication") < builder.index(
        "Scoped builder cleanup"
    )
    assert "retention-days: 3" in builder
    importer = text.split("  importer:\n", 1)[1]
    assert importer.index("Upload importer evidence") < importer.index(
        "Scoped importer cleanup"
    )
    assert "actions/cache" not in text
    assert "docker save" not in text and "docker load" not in text


def test_workflow_steps_are_bounded_and_safe() -> None:
    text = _workflow()
    steps = re.split(r"\n      - ", text.split("jobs:", 1)[1])[1:]
    assert len(steps) >= 25
    for step in steps:
        assert "timeout-minutes:" in step, step.splitlines()[0]
    for banned in (
        "sudo ",
        "continue-on-error",
        "prune",
        "docker rmi -f",
        "image rm -f",
        "! grep",
        "! find",
        "${{ runner.temp }}/saved",
    ):
        assert banned not in text, banned
    assert "persist-credentials: false" in text
    assert text.count("fetch-depth: 0") == 2
    assert '"$PY" scripts/native_saved_image_delivery_acceptance.py builder' in text
    assert '"$PY" scripts/native_saved_image_delivery_acceptance.py importer' in text
