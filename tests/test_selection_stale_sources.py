"""Selection must not let unrelated paths exhaust the freshness warning limit."""

import copy
import os

import pytest

from tests.test_audit24_cli import check, project, save


@pytest.mark.parametrize("unrelated_count", [4, 5, 8])
@pytest.mark.parametrize("shared_path", [None, "dbt_project.yml", "models/schema.yml"])
def test_unrelated_sources_cannot_hide_shared_changes(
    tmp_path, capsys, unrelated_count, shared_path
):
    manifest = project(tmp_path, after="select 1 as id, 2 as tax")
    source_dir = tmp_path / "aaa_unrelated"
    source_dir.mkdir()
    for number in range(unrelated_count):
        name = f"unrelated_{number}"
        node = copy.deepcopy(manifest["nodes"]["model.bookshop.orders"])
        node.update(
            name=name,
            original_file_path=f"aaa_unrelated/{name}.sql",
            raw_code="select 1 as id",
        )
        manifest["nodes"][f"model.bookshop.{name}"] = node
        (source_dir / f"{name}.sql").write_text(node["raw_code"], encoding="utf-8")

    # Sorted source directories put all unrelated model paths before models/
    # and the project file. Only the shared path must affect --select orders.
    save(tmp_path, manifest, base=True)
    save(tmp_path, manifest)
    timestamp = (tmp_path / "target/manifest.json").stat().st_mtime + 2
    for path in source_dir.iterdir():
        os.utime(path, (timestamp, timestamp))
    if shared_path:
        source = tmp_path / shared_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# changed shared configuration\n", encoding="utf-8")
        os.utime(source, (timestamp, timestamp))

    code, report = check(tmp_path, capsys, select="orders")
    assert code == (2 if shared_path else 0), report
    assert report["stale_sources"] == ([shared_path] if shared_path else [])
