"""Tests for manifest parsing and downstream discovery."""

import json

from dbt_plan.manifest import (
    build_node_index,
    find_downstream,
    find_downstream_batch,
    find_node_by_name,
    load_manifest,
)


class TestFindDownstream:
    def test_direct_children(self):
        """Node with direct children returns them."""
        child_map = {
            "model.p.A": ["model.p.B", "model.p.C"],
            "model.p.B": [],
            "model.p.C": [],
        }
        result = find_downstream("model.p.A", child_map)
        assert result == ["model.p.B", "model.p.C"]

    def test_recursive_downstream(self):
        """Transitive dependencies are included."""
        child_map = {
            "model.p.A": ["model.p.B"],
            "model.p.B": ["model.p.C"],
            "model.p.C": [],
        }
        result = find_downstream("model.p.A", child_map)
        assert result == ["model.p.B", "model.p.C"]

    def test_cycle_protection(self):
        """Cycles do not cause infinite loops."""
        child_map = {
            "model.p.A": ["model.p.B"],
            "model.p.B": ["model.p.A"],
        }
        result = find_downstream("model.p.A", child_map)
        assert result == ["model.p.B"]

    def test_filter_test_nodes(self):
        """Test nodes are excluded when models_only=True."""
        child_map = {
            "model.p.A": ["model.p.B", "test.p.not_null_a.abc123"],
            "model.p.B": [],
        }
        result = find_downstream("model.p.A", child_map, models_only=True)
        assert result == ["model.p.B"]

    def test_no_children(self):
        """Leaf node returns empty list."""
        child_map = {"model.p.A": []}
        result = find_downstream("model.p.A", child_map)
        assert result == []


class TestFindNodeByName:
    def test_finds_model_node(self):
        """Finds a model node by its short name."""
        manifest = {
            "nodes": {
                "model.my_project.int_order_enriched": {
                    "name": "int_order_enriched",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                },
            },
        }
        node = find_node_by_name("int_order_enriched", manifest)
        assert node is not None
        assert node.node_id == "model.my_project.int_order_enriched"
        assert node.name == "int_order_enriched"
        assert node.materialization == "incremental"
        assert node.on_schema_change == "sync_all_columns"

    def test_returns_none_when_not_found(self):
        """Returns None for nonexistent model name."""
        manifest = {"nodes": {}}
        assert find_node_by_name("nonexistent", manifest) is None


class TestLoadManifest:
    def test_load_manifest(self, tmp_path):
        """Loads and parses manifest.json."""
        manifest_path = tmp_path / "manifest.json"
        data = {"nodes": {}, "child_map": {}}
        manifest_path.write_text(json.dumps(data))
        result = load_manifest(str(manifest_path))
        assert result["nodes"] == {}
        assert result["child_map"] == {}
        assert result["metadata"] == {}

    def test_load_manifest_filters_extra_keys(self, tmp_path):
        """load_manifest keeps only what it reads, discarding macros/sources/etc."""
        manifest_path = tmp_path / "manifest.json"
        data = {
            "nodes": {"model.p.m": {"name": "m", "config": {}}},
            "child_map": {"model.p.m": []},
            "macros": {"macro.p.x": {}},
            "sources": {"source.p.s": {}},
            "docs": {"doc.p.d": {}},
            "metadata": {"dbt_version": "1.7.0"},
        }
        manifest_path.write_text(json.dumps(data))
        result = load_manifest(str(manifest_path))
        assert set(result.keys()) == {
            "nodes",
            "child_map",
            "metadata",
            "unit_tests",
            "exposures",
            "source_dirs",
        }
        assert "macros" not in result
        assert "sources" not in result
        assert "docs" not in result
        assert result["metadata"] == {"dbt_version": "1.7.0"}

    def test_load_manifest_missing_keys_default_empty(self, tmp_path):
        """load_manifest returns empty dicts when nodes/child_map are absent."""
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({"metadata": {}}))
        result = load_manifest(str(manifest_path))
        assert result["nodes"] == {}
        assert result["child_map"] == {}
        assert result["metadata"] == {}


class TestBuildNodeIndex:
    def test_builds_index_from_model_nodes(self):
        """build_node_index creates name→ModelNode mapping for model nodes."""
        manifest = {
            "nodes": {
                "model.p.dim_customers": {
                    "name": "dim_customers",
                    "config": {
                        "materialized": "table",
                        "on_schema_change": None,
                    },
                },
                "model.p.fct_orders": {
                    "name": "fct_orders",
                    "config": {
                        "materialized": "incremental",
                        "on_schema_change": "sync_all_columns",
                    },
                },
            },
        }
        index = build_node_index(manifest)
        assert len(index) == 2
        assert index["dim_customers"].node_id == "model.p.dim_customers"
        assert index["dim_customers"].materialization == "table"
        assert index["fct_orders"].on_schema_change == "sync_all_columns"

    def test_skips_non_model_nodes(self):
        """build_node_index ignores test, source, seed nodes."""
        manifest = {
            "nodes": {
                "model.p.m": {"name": "m", "config": {"materialized": "table"}},
                "test.p.not_null": {"name": "not_null", "config": {}},
                "seed.p.s": {"name": "s", "config": {}},
            },
        }
        index = build_node_index(manifest)
        assert list(index.keys()) == ["m"]

    def test_first_model_wins_on_duplicate_names(self):
        """build_node_index keeps first model when names collide."""
        manifest = {
            "nodes": {
                "model.p1.dup": {
                    "name": "dup",
                    "config": {"materialized": "table"},
                },
                "model.p2.dup": {
                    "name": "dup",
                    "config": {"materialized": "view"},
                },
            },
        }
        index = build_node_index(manifest)
        assert len(index) == 1
        assert index["dup"].materialization == "table"

    def test_empty_manifest(self):
        """build_node_index returns empty dict for empty manifest."""
        assert build_node_index({}) == {}
        assert build_node_index({"nodes": {}}) == {}

    def test_defaults_materialization_to_table(self):
        """build_node_index defaults materialization to 'table' when missing."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {},
                },
            },
        }
        index = build_node_index(manifest)
        assert index["m"].materialization == "table"
        assert index["m"].on_schema_change is None

    def test_skips_disabled_models(self):
        """build_node_index excludes models with enabled: false."""
        manifest = {
            "nodes": {
                "model.p.active": {
                    "name": "active",
                    "config": {"materialized": "table"},
                },
                "model.p.disabled": {
                    "name": "disabled",
                    "config": {"materialized": "table", "enabled": False},
                },
            },
        }
        index = build_node_index(manifest)
        assert "active" in index
        assert "disabled" not in index

    def test_includes_models_without_enabled_key(self):
        """Models without explicit 'enabled' key are included (default enabled)."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {"materialized": "view"},
                },
            },
        }
        index = build_node_index(manifest)
        assert "m" in index


class TestFindDownstreamBatch:
    def test_batch_matches_individual_calls(self):
        """Batch results match individual find_downstream calls."""
        child_map = {
            "model.p.a": ["model.p.b", "model.p.c"],
            "model.p.b": ["model.p.d"],
            "model.p.c": ["model.p.d"],
            "model.p.d": [],
        }
        node_ids = ["model.p.a", "model.p.b", "model.p.c"]

        batch = find_downstream_batch(node_ids, child_map)
        for nid in node_ids:
            individual = find_downstream(nid, child_map)
            assert batch[nid] == individual, f"Mismatch for {nid}"

    def test_batch_shared_subtrees_efficient(self):
        """Batch correctly handles shared downstream subtrees."""
        child_map = {
            "model.p.a": ["model.p.shared"],
            "model.p.b": ["model.p.shared"],
            "model.p.shared": ["model.p.leaf"],
        }
        batch = find_downstream_batch(["model.p.a", "model.p.b"], child_map)
        assert "model.p.shared" in batch["model.p.a"]
        assert "model.p.leaf" in batch["model.p.a"]
        assert "model.p.shared" in batch["model.p.b"]
        assert "model.p.leaf" in batch["model.p.b"]

    def test_batch_empty_input(self):
        """Empty input returns empty dict."""
        assert find_downstream_batch([], {"a": ["b"]}) == {}


class TestManifestColumns:
    def test_build_node_index_extracts_columns(self):
        """Manifest column definitions are stored in ModelNode."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {"materialized": "incremental"},
                    "columns": {"id": {}, "Name": {}, "EMAIL": {}},
                },
            },
        }
        index = build_node_index(manifest)
        assert index["m"].columns == ("id", "name", "email")  # lowercased

    def test_empty_columns_defaults_to_empty_tuple(self):
        """Models without column definitions get empty tuple."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {"materialized": "table"},
                },
            },
        }
        index = build_node_index(manifest)
        assert index["m"].columns == ()


class TestIncludePackages:
    def test_exclude_packages_filters_non_root(self):
        """include_packages=False excludes models from non-root packages."""
        manifest = {
            "nodes": {
                # Root project has 3 models (most common package)
                "model.myproject.dim_user": {
                    "name": "dim_user",
                    "config": {"materialized": "table"},
                },
                "model.myproject.fct_orders": {
                    "name": "fct_orders",
                    "config": {"materialized": "incremental"},
                },
                "model.myproject.stg_raw": {
                    "name": "stg_raw",
                    "config": {"materialized": "view"},
                },
                # External package model
                "model.dbt_utils.surrogate_key": {
                    "name": "surrogate_key",
                    "config": {"materialized": "ephemeral"},
                },
                # Another external package
                "model.fivetran.stg_stripe": {
                    "name": "stg_stripe",
                    "config": {"materialized": "table"},
                },
            },
        }
        index = build_node_index(manifest, include_packages=False)
        assert sorted(index.keys()) == ["dim_user", "fct_orders", "stg_raw"]
        assert "surrogate_key" not in index
        assert "stg_stripe" not in index

    def test_include_packages_includes_all(self):
        """include_packages=True includes models from all packages."""
        manifest = {
            "nodes": {
                "model.myproject.dim_user": {
                    "name": "dim_user",
                    "config": {"materialized": "table"},
                },
                "model.myproject.fct_orders": {
                    "name": "fct_orders",
                    "config": {"materialized": "incremental"},
                },
                "model.dbt_utils.surrogate_key": {
                    "name": "surrogate_key",
                    "config": {"materialized": "ephemeral"},
                },
                "model.fivetran.stg_stripe": {
                    "name": "stg_stripe",
                    "config": {"materialized": "table"},
                },
            },
        }
        index = build_node_index(manifest, include_packages=True)
        assert sorted(index.keys()) == ["dim_user", "fct_orders", "stg_stripe", "surrogate_key"]

    def test_single_package_all_included_regardless(self):
        """With only one package, include_packages=False still includes all models."""
        manifest = {
            "nodes": {
                "model.myproject.m1": {
                    "name": "m1",
                    "config": {"materialized": "table"},
                },
                "model.myproject.m2": {
                    "name": "m2",
                    "config": {"materialized": "view"},
                },
            },
        }
        index = build_node_index(manifest, include_packages=False)
        assert sorted(index.keys()) == ["m1", "m2"]

    def test_exclude_packages_root_detected_by_most_common(self):
        """Root project is detected as the package with the most models."""
        manifest = {
            "nodes": {
                # 2 models from "app"
                "model.app.a": {
                    "name": "a",
                    "config": {"materialized": "table"},
                },
                "model.app.b": {
                    "name": "b",
                    "config": {"materialized": "table"},
                },
                # 1 model from "pkg" — should be excluded
                "model.pkg.c": {
                    "name": "c",
                    "config": {"materialized": "table"},
                },
            },
        }
        index = build_node_index(manifest, include_packages=False)
        assert sorted(index.keys()) == ["a", "b"]
        assert "c" not in index

    def test_metadata_project_name_used_over_heuristic(self):
        """metadata.project_name is preferred over most-common-package heuristic."""
        manifest = {
            "metadata": {"project_name": "tiny_project"},
            "nodes": {
                # tiny_project has 1 model
                "model.tiny_project.mine": {
                    "name": "mine",
                    "config": {"materialized": "table"},
                },
                # large_package has 3 models — heuristic would pick this
                "model.large_package.a": {
                    "name": "a",
                    "config": {"materialized": "table"},
                },
                "model.large_package.b": {
                    "name": "b",
                    "config": {"materialized": "table"},
                },
                "model.large_package.c": {
                    "name": "c",
                    "config": {"materialized": "table"},
                },
            },
        }
        index = build_node_index(manifest, include_packages=False)
        assert "mine" in index
        assert "a" not in index


class TestMaterializedNull:
    def test_materialized_null_defaults_to_table(self):
        """materialized: null in manifest should default to 'table'."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {"materialized": None},
                },
            },
        }
        index = build_node_index(manifest)
        assert index["m"].materialization == "table"

    def test_find_node_by_name_materialized_null(self):
        """find_node_by_name also defaults null materialized to 'table'."""
        manifest = {
            "nodes": {
                "model.p.m": {
                    "name": "m",
                    "config": {"materialized": None},
                },
            },
        }
        node = find_node_by_name("m", manifest)
        assert node is not None
        assert node.materialization == "table"


class TestCycleSafety:
    def test_batch_with_cycle_no_self_reference(self):
        """Cyclic child_map: a node should not appear in its own downstream."""
        child_map = {
            "model.proj.a": ["model.proj.b"],
            "model.proj.b": ["model.proj.a"],
        }
        result = find_downstream_batch(["model.proj.a", "model.proj.b"], child_map)
        assert "model.proj.a" not in result["model.proj.a"]
        assert "model.proj.b" not in result["model.proj.b"]

    def test_self_loop(self):
        """Node pointing to itself should have empty downstream."""
        child_map = {"model.proj.a": ["model.proj.a"]}
        result = find_downstream_batch(["model.proj.a"], child_map)
        assert result["model.proj.a"] == []


class TestLoadManifestMetadata:
    def test_metadata_preserved(self, tmp_path):
        """load_manifest preserves metadata section."""
        manifest = {
            "metadata": {"project_name": "my_project", "dbt_version": "1.8.0"},
            "nodes": {},
            "child_map": {},
        }
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(manifest))
        result = load_manifest(path)
        assert result["metadata"]["project_name"] == "my_project"
