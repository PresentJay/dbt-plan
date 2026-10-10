"""Independent byte-level controls for snapshot integrity and its CLI boundary."""

import hashlib
import json
from pathlib import Path

import pytest

from dbt_plan.cli import _do_check, _do_snapshot
from tests.test_snapshot_lifecycle import (
    _check_args,
    _minimal_manifest,
    _setup_target,
    _snapshot_args,
)

INVENTORY = "inventory.json"


@pytest.fixture
def project(tmp_path, capsys):
    _setup_target(tmp_path, {"orders": "select 1 as id"}, _minimal_manifest({"orders": {}}))
    _do_snapshot(_snapshot_args(tmp_path))
    capsys.readouterr()
    return tmp_path


def base(project):
    return project / ".dbt-plan/base"


def inventory(project):
    return json.loads((base(project) / INVENTORY).read_text(encoding="utf-8"))


def save(project, value):
    (base(project) / INVENTORY).write_text(json.dumps(value), encoding="utf-8")


def bytes_in(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def refused(project, capsys):
    args = _check_args(project)
    args.select = "unknown+"  # Integrity errors precede selection and report generation.
    assert _do_check(args) == 3
    output = capsys.readouterr()
    assert output.out == ""
    assert "integrity" in output.err.lower()
    assert "dbt-plan snapshot" in output.err


def test_clean_capture_independent_hashes_and_read_only_check(project, capsys):
    expected = []
    for name in ["compiled/models/orders.sql", "manifest.json"]:
        expected.append(
            {
                "path": name,
                "sha256": hashlib.sha256((base(project) / name).read_bytes()).hexdigest(),
            }
        )
    assert inventory(project) == {"version": 1, "files": expected}
    before = bytes_in(base(project))
    assert _do_check(_check_args(project)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["analysis"]["baseline"]["integrity"] == "verified"
    assert report["analysis"]["baseline"]["layout_version"] == 1
    assert bytes_in(base(project)) == before


def test_legacy_is_unrecorded_even_if_provenance_claims_verified(project, capsys):
    (base(project) / INVENTORY).unlink(missing_ok=True)
    provenance = base(project) / "provenance.json"
    provenance.write_text('{"integrity": "verified"}')
    before = bytes_in(base(project))
    assert _do_check(_check_args(project)) == 0
    assert json.loads(capsys.readouterr().out)["analysis"]["baseline"]["integrity"] == "unrecorded"
    assert bytes_in(base(project)) == before


@pytest.mark.parametrize("name", ["compiled/models/orders.sql", "manifest.json"])
@pytest.mark.parametrize("damage", ["byte", "missing"])
def test_changed_or_missing_input_refused(project, capsys, name, damage):
    path = base(project) / name
    if damage == "missing":
        path.unlink()
    else:
        raw = path.read_bytes()
        path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    refused(project, capsys)


@pytest.mark.parametrize(
    "name", ["compiled/models/extra.sql", "compiled/tests/extra.sql", "extra.sql"]
)
def test_unexpected_sql_refused(project, capsys, name):
    path = base(project) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("select 1")
    refused(project, capsys)


@pytest.mark.parametrize(
    "raw", ["{", "[]", "null", "\xff", '{"version":1,"version":1,"files":[]}']
)
def test_corrupt_inventory_is_not_legacy(project, capsys, raw):
    (base(project) / INVENTORY).write_bytes(raw.encode("latin-1"))
    refused(project, capsys)


@pytest.mark.parametrize("version", [2, 0, -1, True, False, None, "1", 1.0, [], {}])
def test_unknown_version_or_type_refused(project, capsys, version):
    value = inventory(project)
    value["version"] = version
    save(project, value)
    refused(project, capsys)


@pytest.mark.parametrize(
    "digest", [None, True, 42, [], "", "a" * 63, "a" * 65, "g" * 64, "A" * 64]
)
def test_malformed_digest_refused(project, capsys, digest):
    value = inventory(project)
    value["files"][0]["sha256"] = digest
    save(project, value)
    refused(project, capsys)


@pytest.mark.parametrize(
    "path",
    [
        None,
        True,
        [],
        "",
        "/tmp/escape.sql",
        "../escape.sql",
        "compiled/../escape.sql",
        "./compiled/models/orders.sql",
        "compiled//models/orders.sql",
        "compiled/models/orders.sql/",
        r"compiled\models\orders.sql",
        "C:/escape.sql",
        "C:escape.sql",
        "compiled/models/a:stream.sql",
        "compiled/models/a\x00.sql",
        "compiled/models/a\n.sql",
        "inventory.json",
    ],
)
def test_invalid_paths_refused(project, capsys, path):
    value = inventory(project)
    value["files"][0]["path"] = path
    save(project, value)
    refused(project, capsys)


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate",
        "unsorted",
        "missing-entry",
        "extra-field",
        "missing-version",
        "bad-files",
        "bad-entry",
        "entry-extra",
    ],
)
def test_entire_schema_is_validated(project, capsys, damage):
    value = inventory(project)
    if damage == "duplicate":
        value["files"].append(value["files"][0])
    elif damage == "unsorted":
        value["files"].reverse()
    elif damage == "missing-entry":
        value["files"].pop()
    elif damage == "extra-field":
        value["algorithm"] = "sha256"
    elif damage == "missing-version":
        del value["version"]
    elif damage == "bad-files":
        value["files"] = {}
    elif damage == "bad-entry":
        value["files"][0] = None
    else:
        value["files"][0]["ignored"] = True
    save(project, value)
    refused(project, capsys)


def test_unicode_paths_and_all_regular_compiled_sql(project, capsys):
    root = project / "target/compiled/my_project"
    for name in ["models/주문.sql", "tests/é.sql", "models/schema.yml/check.sql"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("select 1 as id", encoding="utf-8")
    _do_snapshot(_snapshot_args(project))
    capsys.readouterr()
    entries = inventory(project)["files"]
    assert [e["path"] for e in entries] == sorted(e["path"] for e in entries)
    assert len(entries) == 5
    for entry in entries:
        assert (
            entry["sha256"]
            == hashlib.sha256((base(project) / entry["path"]).read_bytes()).hexdigest()
        )
    assert _do_check(_check_args(project)) == 0
    assert json.loads(capsys.readouterr().out)["analysis"]["baseline"]["integrity"] == "verified"


@pytest.mark.parametrize(
    "name",
    [
        "inventory.json",
        "manifest.json",
        "compiled",
        "compiled/models",
        "compiled/models/orders.sql",
        "compiled/models/new.sql",
    ],
)
@pytest.mark.parametrize("outside", [False, True])
def test_symlinks_fail_closed_without_reading_targets(project, capsys, monkeypatch, name, outside):
    import shutil

    path = base(project) / name
    directory = path.is_dir()
    target = project / "outside" if outside else base(project) / "link-target"
    if path.exists():
        if directory:
            shutil.copytree(path, target)
            shutil.rmtree(path)
        else:
            target.write_bytes(path.read_bytes())
            path.unlink()
    path.symlink_to(target, target_is_directory=directory)
    original = Path.open

    def guarded(p, *args, **kwargs):
        assert p != target and not p.is_symlink(), "must not open a symlink target"
        return original(p, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    refused(project, capsys)


def test_large_file_hashes_in_bounded_chunks(project, monkeypatch):
    from dbt_plan.snapshot_integrity import validate_snapshot_inventory, write_snapshot_inventory

    path = base(project) / "compiled/models/orders.sql"
    content = b"select 1 as id;\n" * 200000
    path.write_bytes(content)
    original = Path.open
    reads = []

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            reads.append(size)
            return self.stream.read(size)

    def bounded(p, *args, **kwargs):
        stream = original(p, *args, **kwargs)
        return BoundedReader(stream) if p == path else stream

    monkeypatch.setattr(Path, "open", bounded)
    write_snapshot_inventory(base(project))
    assert validate_snapshot_inventory(base(project)) == "verified"
    assert len(reads) > 4
    assert inventory(project)["files"][0]["sha256"] == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize("fault", ["hash", "write"])
@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_inventory_failure_preserves_old_bytes_and_retry(project, monkeypatch, fault, error):
    from dbt_plan import snapshot_integrity

    before = bytes_in(base(project))
    (project / "target/compiled/my_project/models/orders.sql").write_text("select 2 as id")
    with monkeypatch.context() as patch:
        if fault == "hash":

            def fail(*args, **kwargs):
                raise error("hash failed")

            patch.setattr(snapshot_integrity, "_sha256", fail)
        else:
            original = Path.write_text

            def write(path, *args, **kwargs):
                if path.name == INVENTORY:
                    original(path, "partial")
                    raise error("inventory write failed")
                return original(path, *args, **kwargs)

            patch.setattr(Path, "write_text", write)
        with pytest.raises(OSError):
            _do_snapshot(_snapshot_args(project))
        assert bytes_in(base(project)) == before
        assert list(base(project).parent.iterdir()) == [base(project)]
    _do_snapshot(_snapshot_args(project))
    assert snapshot_integrity.validate_snapshot_inventory(base(project)) == "verified"
    assert (base(project) / "compiled/models/orders.sql").read_text() == "select 2 as id"


def test_missing_manifest_at_capture_never_verified(project, capsys):
    before = bytes_in(base(project))
    (project / "target/manifest.json").unlink()
    with pytest.raises(SystemExit) as exc:
        _do_snapshot(_snapshot_args(project))
    assert exc.value.code == 3
    assert bytes_in(base(project)) == before
    assert "dbt compile" in capsys.readouterr().err


def test_uppercase_sql_is_inventoried_on_every_platform(project, capsys):
    path = project / "target/compiled/my_project/models/upper.SQL"
    path.write_text("select 1")
    _do_snapshot(_snapshot_args(project))
    capsys.readouterr()
    assert "compiled/models/upper.SQL" in [entry["path"] for entry in inventory(project)["files"]]
    (base(project) / "compiled/models/upper.SQL").write_text("select 2")
    refused(project, capsys)


def test_symlink_snapshot_root_refused(project, capsys):
    root = base(project)
    outside = project / "elsewhere"
    root.rename(outside)
    root.symlink_to(outside, target_is_directory=True)
    refused(project, capsys)


@pytest.mark.parametrize(
    "path",
    [
        "compiled/models/NUL.sql",
        "compiled/CON/orders.sql",
        "compiled/models/a?.sql",
        "compiled/models/a /other.sql",
        "compiled/models/a./other.sql",
        "compiled/models/\ud800.sql",
    ],
)
def test_nonportable_paths_refused(project, capsys, path):
    value = inventory(project)
    value["files"][0]["path"] = path
    save(project, value)
    refused(project, capsys)


def test_corrupt_inventory_precedes_current_input_failure(project, capsys):
    (base(project) / INVENTORY).write_text("{")
    (project / "target/manifest.json").unlink()
    refused(project, capsys)


def test_parse_failure_remains_warning_with_verified_bytes(project, capsys):
    manifest = _minimal_manifest(
        {"orders": {"materialized": "incremental", "on_schema_change": "sync_all_columns"}}
    )
    _setup_target(project, {"orders": "select 1 as id"}, manifest)
    _do_snapshot(_snapshot_args(project))
    (project / "target/compiled/my_project/models/orders.sql").write_text("select from (")
    capsys.readouterr()
    assert _do_check(_check_args(project)) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["analysis"]["baseline"]["integrity"] == "verified"
    assert report["models"][0]["safety"] != "safe"


def test_all_entries_validated_before_hashing(project, capsys, monkeypatch):
    from dbt_plan import snapshot_integrity

    value = inventory(project)
    value["files"][-1]["sha256"] = "bad"
    save(project, value)

    def must_not_hash(*args):
        pytest.fail("Invalid inventories must not be partially verified")

    monkeypatch.setattr(snapshot_integrity, "_sha256", must_not_hash)
    refused(project, capsys)


def test_windows_junction_attributes_fail_closed(project, capsys, monkeypatch):
    import stat
    from types import SimpleNamespace

    original = Path.lstat
    linked = base(project) / "compiled/models"

    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == linked:
            return SimpleNamespace(
                st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    refused(project, capsys)


@pytest.mark.parametrize("name", ["inventory.json", "compiled/models/orders.sql"])
def test_unreadable_input_is_execution_error(project, capsys, monkeypatch, name):
    original = Path.open

    def unreadable(path, *args, **kwargs):
        if path == base(project) / name:
            raise PermissionError("injected unreadable file")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    refused(project, capsys)


def test_directory_named_sql_is_rejected_before_analysis(project, capsys):
    (base(project) / "compiled/models/new.sql").mkdir()
    refused(project, capsys)


def test_special_metadata_file_refused_before_open(project, capsys, monkeypatch):
    import stat
    from types import SimpleNamespace

    original = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == base(project) / "provenance.json":
            return SimpleNamespace(st_mode=stat.S_IFIFO)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    refused(project, capsys)


@pytest.mark.parametrize("damage", ["inventory", "manifest", "missing-sql"])
@pytest.mark.parametrize("interface", ["cli", "mcp"])
def test_real_cli_and_mcp_refuse_inventoried_damage(project, monkeypatch, damage, interface):
    import os
    import subprocess
    import sys

    for key in os.environ:
        if key.startswith("DBT_PLAN_"):
            monkeypatch.delenv(key)

    def invoke(*args):
        return subprocess.run(
            [sys.executable, "-m", "dbt_plan.cli", *args, "--project-dir", str(project)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )

    # Exercise a real capture and successful report before corrupting its inventory/input.
    captured = invoke("snapshot")
    assert captured.returncode == 0, captured.stderr
    if interface == "mcp":
        server = pytest.importorskip("dbt_plan_mcp.server", reason="needs the optional mcp extra")
        assert server.plan(str(project))["verdict"] == "safe"
    else:
        clean = invoke("check", "--format", "json")
        assert clean.returncode == 0, clean.stderr
        assert json.loads(clean.stdout)["analysis"]["baseline"]["integrity"] == "verified"

    if damage == "inventory":
        (base(project) / INVENTORY).write_text("{")
    elif damage == "manifest":
        (base(project) / "manifest.json").write_text("{")
    else:
        (base(project) / "compiled/models/orders.sql").unlink()
    # A permissive warning policy cannot turn an integrity error into success.
    (project / ".dbt-plan.yml").write_text("warning_exit_code: 0\n")
    if interface == "mcp":
        result = server.plan(str(project))
        assert result["verdict"] == "error"
        assert "integrity" in result["error"].lower()
        assert "summary" not in result and "models" not in result
    else:
        result = invoke("check", "--format", "json")
        assert result.returncode == 3
        assert result.stdout == ""
        assert "integrity" in result.stderr.lower()
