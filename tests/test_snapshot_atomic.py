"""Fault injection for durable snapshot publication."""

import argparse
import json
import shutil
import sys
from pathlib import Path

import pytest

from dbt_plan.cli import _do_snapshot, main


def setup_project(project):
    models = project / "target/compiled/shop/models"
    models.mkdir(parents=True)
    (models / "orders.sql").write_text("select 1 as id", encoding="utf-8")
    (project / "target/manifest.json").write_text(
        json.dumps({"metadata": {"project_name": "shop"}, "nodes": {}}), encoding="utf-8"
    )
    return argparse.Namespace(project_dir=str(project), target_dir="target")


def contents(directory):
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize("fault", ["sql", "manifest", "provenance"])
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_failed_build_preserves_old_baseline(tmp_path, monkeypatch, capsys, fault, error):
    args = setup_project(tmp_path)
    _do_snapshot(args)
    base = tmp_path / ".dbt-plan/base"
    before = contents(base)
    (tmp_path / "target/compiled/shop/models/orders.sql").write_text("select 2 as id")
    capsys.readouterr()

    def fail(*args, **kwargs):
        raise error("injected build failure")

    with monkeypatch.context() as patch:
        if fault == "sql":
            patch.setattr(shutil, "copytree", fail)
        elif fault == "manifest":
            patch.setattr(shutil, "copy2", fail)
        else:
            original = Path.write_text

            def write(path, *args, **kwargs):
                if path.name == "provenance.json":
                    fail()
                return original(path, *args, **kwargs)

            patch.setattr(Path, "write_text", write)
        patch.setattr(sys, "argv", ["dbt-plan", "snapshot", "--project-dir", str(tmp_path)])
        with pytest.raises((SystemExit, KeyboardInterrupt)) as exc:
            main()
        assert contents(base) == before
        assert isinstance(exc.value, SystemExit) and exc.value.code == 3
        assert "Snapshot saved" not in capsys.readouterr().err
        assert list(base.parent.iterdir()) == [base]

    _do_snapshot(args)
    assert (base / "compiled/models/orders.sql").read_text() == "select 2 as id"


@pytest.mark.parametrize("fault", ["old", "publish"])
@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt, SystemExit])
def test_rename_failure_restores_bytes(tmp_path, monkeypatch, capsys, fault, after, error):
    args = setup_project(tmp_path)
    _do_snapshot(args)
    base = tmp_path / ".dbt-plan/base"
    before = contents(base)
    original = Path.rename
    fired = False

    def rename(path, destination):
        nonlocal fired
        matches = path == base if fault == "old" else path.name.startswith(".snapshot-stage-")
        if matches and not fired:
            fired = True
            if after:
                original(path, destination)
            raise error("injected rename failure")
        return original(path, destination)

    capsys.readouterr()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", rename)
        patch.setattr(sys, "argv", ["dbt-plan", "snapshot", "--project-dir", str(tmp_path)])
        with pytest.raises(SystemExit) as exc:
            main()
        if error is not SystemExit:
            assert exc.value.code == 3
        assert fired
        assert contents(base) == before
        assert list(base.parent.iterdir()) == [base]
        assert "Snapshot saved" not in capsys.readouterr().err
    _do_snapshot(args)


@pytest.mark.parametrize("rollback_error", [OSError, KeyboardInterrupt, SystemExit])
def test_failed_rollback_keeps_exact_recovery_path(tmp_path, monkeypatch, capsys, rollback_error):
    args = setup_project(tmp_path)
    _do_snapshot(args)
    base = tmp_path / ".dbt-plan/base"
    before = contents(base)
    original = Path.rename

    def rename(path, destination):
        if path.name.startswith(".snapshot-stage-"):
            raise OSError("publication blocked")
        if path.parent.name.startswith(".snapshot-backup-"):
            raise rollback_error("rollback blocked")
        return original(path, destination)

    capsys.readouterr()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", rename)
        patch.setattr(sys, "argv", ["dbt-plan", "snapshot", "--project-dir", str(tmp_path)])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 3
    backups = list(base.parent.glob(".snapshot-backup-*/base"))
    assert len(backups) == 1
    assert contents(backups[0]) == before
    message = capsys.readouterr().err
    assert str(backups[0]) in message
    assert "rollback failed" in message and "Snapshot saved" not in message
    assert not list(base.parent.glob(".snapshot-stage-*"))
    # A retry does not delete a previously retained recovery copy.
    _do_snapshot(args)
    assert contents(backups[0]) == before


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_first_snapshot_failure_and_retry(tmp_path, monkeypatch, capsys, error):
    args = setup_project(tmp_path)
    original = shutil.copytree

    def partial_copy(source, destination, **kwargs):
        original(source, destination, **kwargs)
        raise error("partial copy")

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "copytree", partial_copy)
        patch.setattr(sys, "argv", ["dbt-plan", "snapshot", "--project-dir", str(tmp_path)])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 3
    assert not list((tmp_path / ".dbt-plan").iterdir())
    assert "Snapshot saved" not in capsys.readouterr().err
    _do_snapshot(args)
    assert (tmp_path / ".dbt-plan/base/provenance.json").is_file()


def test_success_replaces_complete_tree_and_metadata(tmp_path):
    args = setup_project(tmp_path)
    _do_snapshot(args)
    models = tmp_path / "target/compiled/shop/models"
    (models / "orders.sql").unlink()
    (models / "books.sql").write_text("select 2 as book_id")
    manifest = tmp_path / "target/manifest.json"
    manifest.write_text('{"metadata": {"project_name": "shop"}, "nodes": {}, "new": true}')
    _do_snapshot(args)
    base = tmp_path / ".dbt-plan/base"
    assert set(contents(base)) == {"compiled/models/books.sql", "manifest.json", "provenance.json"}
    assert (base / "manifest.json").read_bytes() == manifest.read_bytes()
    assert set(json.loads((base / "provenance.json").read_text())) == {
        "revision",
        "created_at",
        "dbt_plan_version",
    }
    assert list(base.parent.iterdir()) == [base]


def test_manifest_only_replacement(tmp_path):
    args = setup_project(tmp_path)
    _do_snapshot(args)
    shutil.rmtree(tmp_path / "target/compiled")
    manifest = {
        "metadata": {"project_name": "shop"},
        "nodes": {
            "snapshot.shop.books": {"resource_type": "snapshot", "config": {"enabled": True}}
        },
    }
    (tmp_path / "target/manifest.json").write_text(json.dumps(manifest))
    _do_snapshot(args)
    base = tmp_path / ".dbt-plan/base"
    assert (base / "compiled").is_dir()
    assert list((base / "compiled").iterdir()) == []
    assert json.loads((base / "manifest.json").read_text()) == manifest
    assert (base / "provenance.json").is_file()


def test_missing_manifest_keeps_warning_contract(tmp_path, capsys):
    args = setup_project(tmp_path)
    _do_snapshot(args)
    (tmp_path / "target/manifest.json").unlink()
    capsys.readouterr()
    _do_snapshot(args)
    message = capsys.readouterr().err
    assert "Warning: manifest.json not found" in message
    assert "Without it, 'dbt-plan check' will fail." in message
    assert "Snapshot saved" in message
    assert not (tmp_path / ".dbt-plan/base/manifest.json").exists()


@pytest.mark.parametrize("invalid", ["missing", "ambiguous"])
def test_early_layout_error_preserves_baseline(tmp_path, monkeypatch, capsys, invalid):
    args = setup_project(tmp_path)
    _do_snapshot(args)
    base = tmp_path / ".dbt-plan/base"
    before = contents(base)
    if invalid == "missing":
        shutil.rmtree(tmp_path / "target/compiled")
    else:
        (tmp_path / "target/compiled/other/models").mkdir(parents=True)
        (tmp_path / "target/manifest.json").unlink()
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["dbt-plan", "snapshot", "--project-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 3
    assert contents(base) == before
    assert list(base.parent.iterdir()) == [base]
    assert "Snapshot saved" not in capsys.readouterr().err


def symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


@pytest.mark.parametrize("where", ["base", "parent"])
@pytest.mark.parametrize("target_kind", ["outside", "dangling", "project"])
def test_destination_symlink_escape_refused(tmp_path, monkeypatch, capsys, where, target_kind):
    project = tmp_path / "project"
    setup_project(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"untouched")
    target = {"outside": outside, "dangling": tmp_path / "absent", "project": project}[target_kind]
    parent = project / ".dbt-plan"
    if where == "base":
        parent.mkdir()
    symlink(parent / "base" if where == "base" else parent, target, directory=True)
    monkeypatch.setattr(sys, "argv", ["dbt-plan", "snapshot", "--project-dir", str(project)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 3
    assert (outside / "keep").read_bytes() == b"untouched"
    assert not (tmp_path / "absent").exists()
    assert "Snapshot saved" not in capsys.readouterr().err
    assert not list(outside.glob(".snapshot-*"))


def test_compiled_symlinks_are_preserved_not_followed(tmp_path):
    project = tmp_path / "project"
    args = setup_project(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.sql").write_text("outside bytes")
    models = project / "target/compiled/shop/models"
    symlink(models / "external", outside, directory=True)
    symlink(models / "missing.sql", tmp_path / "absent.sql")
    _do_snapshot(args)
    base = project / ".dbt-plan/base"
    assert (base / "compiled/models/external").is_symlink()
    assert (base / "compiled/models/missing.sql").is_symlink()
    _do_snapshot(args)
    assert (outside / "secret.sql").read_text() == "outside bytes"


@pytest.mark.parametrize("linked", [False, True])
def test_file_baseline_rollback(tmp_path, monkeypatch, linked):
    args = setup_project(tmp_path)
    base = tmp_path / ".dbt-plan/base"
    base.parent.mkdir()
    original_file = tmp_path / "old.txt"
    original_file.write_bytes(b"old baseline")
    if linked:
        symlink(base, Path("../old.txt"))
    else:
        base.write_bytes(b"old baseline")
    original = Path.rename

    def rename(path, destination):
        if path.name.startswith(".snapshot-stage-"):
            raise OSError("publish failed")
        return original(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", rename)
        with pytest.raises(OSError):
            _do_snapshot(args)
    assert base.read_bytes() == b"old baseline"
    assert base.is_symlink() == linked
    _do_snapshot(args)
    assert base.is_dir() and not base.is_symlink()
    assert original_file.read_bytes() == b"old baseline"


@pytest.mark.parametrize("after", [False, True])
def test_first_publication_interrupt_leaves_no_partial_baseline(tmp_path, monkeypatch, after):
    args = setup_project(tmp_path)
    original = Path.rename
    fired = False

    def rename(path, destination):
        nonlocal fired
        if not fired:
            fired = True
            if after:
                original(path, destination)
            raise KeyboardInterrupt()
        return original(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", rename)
        with pytest.raises(OSError, match="interrupted"):
            _do_snapshot(args)
    assert not list((tmp_path / ".dbt-plan").iterdir())
    _do_snapshot(args)


def test_cleanup_failure_does_not_hide_recovery_path(tmp_path, monkeypatch, capsys):
    args = setup_project(tmp_path)
    _do_snapshot(args)
    base = tmp_path / ".dbt-plan/base"
    before = contents(base)
    original = Path.rename

    def rename(path, destination):
        if path != base:
            raise KeyboardInterrupt("rename blocked")
        return original(path, destination)

    def cleanup(path, *args, **kwargs):
        raise OSError("cleanup blocked")

    capsys.readouterr()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", rename)
        patch.setattr(shutil, "rmtree", cleanup)
        patch.setattr(sys, "argv", ["dbt-plan", "snapshot", "--project-dir", str(tmp_path)])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 3
    backup = next(base.parent.glob(".snapshot-backup-*/base"))
    assert contents(backup) == before
    message = capsys.readouterr().err
    assert str(backup) in message and "cleanup failed" in message
    assert "Snapshot saved" not in message


def test_post_commit_cleanup_failure_keeps_new_baseline(tmp_path, monkeypatch, capsys):
    args = setup_project(tmp_path)
    _do_snapshot(args)
    (tmp_path / "target/compiled/shop/models/orders.sql").write_text("select 2 as id")
    original = shutil.rmtree

    def cleanup(path, *args, **kwargs):
        if Path(path).name.startswith(".snapshot-backup-"):
            raise OSError("backup cleanup blocked")
        return original(path, *args, **kwargs)

    capsys.readouterr()
    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", cleanup)
        patch.setattr(sys, "argv", ["dbt-plan", "snapshot", "--project-dir", str(tmp_path)])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 3
    base = tmp_path / ".dbt-plan/base"
    assert (base / "compiled/models/orders.sql").read_text() == "select 2 as id"
    backup = next(base.parent.glob(".snapshot-backup-*"))
    message = capsys.readouterr().err
    assert str(backup) in message and "Snapshot published" in message
    assert "Snapshot saved" not in message


@pytest.mark.parametrize("dangling", [False, True])
def test_internal_directory_or_dangling_base_symlink_refused(tmp_path, dangling):
    args = setup_project(tmp_path)
    base = tmp_path / ".dbt-plan/base"
    base.parent.mkdir()
    target = tmp_path / "original"
    if not dangling:
        target.mkdir()
        (target / "keep").write_bytes(b"keep")
    symlink(base, target, directory=True)
    with pytest.raises(SystemExit) as exc:
        _do_snapshot(args)
    assert exc.value.code == 3
    assert base.is_symlink()
    if not dangling:
        assert (target / "keep").read_bytes() == b"keep"


def test_relative_project_and_internal_parent_symlink(tmp_path, monkeypatch):
    project = tmp_path / "project"
    setup_project(project)
    storage = project / "storage"
    storage.mkdir()
    symlink(project / ".dbt-plan", storage, directory=True)
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(project_dir="project", target_dir="target")
    _do_snapshot(args)
    _do_snapshot(args)
    assert (storage / "base/compiled/models/orders.sql").read_text() == "select 1 as id"
    assert list(storage.iterdir()) == [storage / "base"]


def test_restore_completed_before_interrupt_reports_absolute_path(tmp_path, monkeypatch):
    project = tmp_path / "project"
    setup_project(project)
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(project_dir="project", target_dir="target")
    _do_snapshot(args)
    base = project / ".dbt-plan/base"
    before = contents(base)
    original = Path.rename

    def rename(path, destination):
        if path.name.startswith(".snapshot-stage-"):
            raise OSError("publish blocked")
        result = original(path, destination)
        if path.parent.name.startswith(".snapshot-backup-"):
            raise KeyboardInterrupt("after restore")
        return result

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError) as exc:
        _do_snapshot(args)
    assert str(base) in str(exc.value)
    assert contents(base) == before
