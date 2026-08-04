"""Unit tests for static Alembic revision graph discovery."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from fetchnow_pg_backup.alembic_graph import (  # noqa: E402
    AlembicGraphError,
    discover_alembic_head_revisions,
)


def _write(versions: Path, name: str, body: str) -> None:
    (versions / name).write_text(body, encoding="utf-8")


def test_linear_chain(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(
        versions,
        "0001_a.py",
        'revision = "0001_a"\ndown_revision = None\n',
    )
    _write(
        versions,
        "0002_b.py",
        'revision = "0002_b"\ndown_revision = "0001_a"\n',
    )
    assert discover_alembic_head_revisions(versions) == ("0002_b",)


def test_two_independent_heads(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(versions, "0001_a.py", 'revision = "0001_a"\ndown_revision = None\n')
    _write(versions, "0002_b.py", 'revision = "0002_b"\ndown_revision = None\n')
    assert discover_alembic_head_revisions(versions) == ("0001_a", "0002_b")


def test_branch_and_merge_tuple(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(versions, "0001_root.py", 'revision = "0001_root"\ndown_revision = None\n')
    _write(
        versions,
        "0002_a.py",
        'revision = "0002_a"\ndown_revision = "0001_root"\n',
    )
    _write(
        versions,
        "0002_b.py",
        'revision = "0002_b"\ndown_revision = "0001_root"\n',
    )
    _write(
        versions,
        "0003_merge.py",
        'revision = "0003_merge"\ndown_revision = ("0002_a", "0002_b")\n',
    )
    assert discover_alembic_head_revisions(versions) == ("0003_merge",)


def test_branch_and_merge_list(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(versions, "0001_root.py", 'revision = "0001_root"\ndown_revision = None\n')
    _write(
        versions,
        "0002_a.py",
        'revision = "0002_a"\ndown_revision = "0001_root"\n',
    )
    _write(
        versions,
        "0002_b.py",
        'revision = "0002_b"\ndown_revision = "0001_root"\n',
    )
    _write(
        versions,
        "0003_merge.py",
        'revision = "0003_merge"\ndown_revision = ["0002_a", "0002_b"]\n',
    )
    assert discover_alembic_head_revisions(versions) == ("0003_merge",)


def test_duplicate_revision(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(versions, "0001_a.py", 'revision = "0001_a"\ndown_revision = None\n')
    _write(versions, "0001_dup.py", 'revision = "0001_a"\ndown_revision = None\n')
    with pytest.raises(AlembicGraphError, match="duplicate"):
        discover_alembic_head_revisions(versions)


def test_missing_parent(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(
        versions,
        "0002_b.py",
        'revision = "0002_b"\ndown_revision = "0001_missing"\n',
    )
    with pytest.raises(AlembicGraphError, match="missing parent"):
        discover_alembic_head_revisions(versions)


def test_self_reference(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(
        versions,
        "0001_a.py",
        'revision = "0001_a"\ndown_revision = "0001_a"\n',
    )
    with pytest.raises(AlembicGraphError, match="self-references"):
        discover_alembic_head_revisions(versions)


def test_multi_node_cycle(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(
        versions,
        "0001_a.py",
        'revision = "0001_a"\ndown_revision = "0002_b"\n',
    )
    _write(
        versions,
        "0002_b.py",
        'revision = "0002_b"\ndown_revision = "0001_a"\n',
    )
    with pytest.raises(AlembicGraphError, match="cycle"):
        discover_alembic_head_revisions(versions)


def test_dynamic_metadata_rejected(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(
        versions,
        "0001_a.py",
        "revision = f\"{'0001_a'}\"\ndown_revision = None\n",
    )
    with pytest.raises(AlembicGraphError):
        discover_alembic_head_revisions(versions)


def test_malformed_parent_member(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(versions, "0001_root.py", 'revision = "0001_root"\ndown_revision = None\n')
    _write(
        versions,
        "0002_merge.py",
        "revision = \"0002_merge\"\ndown_revision = (\"0001_root\", 123)\n",
    )
    with pytest.raises(AlembicGraphError):
        discover_alembic_head_revisions(versions)


def test_real_world_annassign_migration_format(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(
        versions,
        "0001_baseline.py",
        'revision: str = "0001_baseline"\n'
        "down_revision: Union[str, None] = None\n"
        "depends_on: Union[str, Sequence[str], None] = None\n",
    )
    assert discover_alembic_head_revisions(versions) == ("0001_baseline",)


def test_incomplete_migration_file_fails_closed(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(versions, "0001_a.py", 'revision = "0001_a"\n')
    with pytest.raises(AlembicGraphError, match="down_revision"):
        discover_alembic_head_revisions(versions)


def test_depends_on_does_not_change_heads(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write(versions, "0001_a.py", 'revision = "0001_a"\ndown_revision = None\n')
    _write(
        versions,
        "0002_b.py",
        'revision = "0002_b"\ndown_revision = "0001_a"\n'
        'depends_on = ("0001_a",)\n',
    )
    assert discover_alembic_head_revisions(versions) == ("0002_b",)
