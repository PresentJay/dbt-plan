"""dbt unit tests downstream of a column drop.

A unit test pins its columns down by hand, in `expect` and in every `given`
input. dbt checks each of those against the real relation it stands in for and
errors on a name that is not there:

    Invalid column name: 'customer_id' in unit test fixture for 'stg_orders'.
    Accepted columns for 'stg_orders' are: ['order_id', 'store_id', 'order_date']

so dropping a column breaks them at `dbt build`. Both messages above were
produced by dbt 1.11.7 against tests/dbt_project; see test_dbt_e2e.py, which
runs the build and asserts on the real failure rather than on this description
of it.
"""

from __future__ import annotations

import pytest

from dbt_plan.manifest import _input_model, build_unit_test_index
from dbt_plan.predictor import DDLPrediction, Safety, analyze_cascade_impacts, predict_ddl


def _manifest(*unit_tests: dict) -> dict:
    return {"unit_tests": {ut["unique_id"]: ut for ut in unit_tests}}


def _unit_test(
    unique_id: str,
    model: str,
    expect: dict | None = None,
    given: list[dict] | None = None,
    enabled: bool = True,
) -> dict:
    return {
        "unique_id": unique_id,
        "name": unique_id.split(".")[-1],
        "model": model,
        "expect": expect if expect is not None else {"format": "dict", "rows": []},
        "given": given or [],
        "config": {"enabled": enabled},
    }


class TestReadingFixtures:
    def test_dict_rows_give_their_keys(self):
        index = build_unit_test_index(
            _manifest(
                _unit_test(
                    "unit_test.p.stg.t",
                    "stg",
                    expect={"format": "dict", "rows": [{"a": 1}, {"B": 2}]},
                )
            )
        )
        expect = index["unit_test.p.stg.t"].fixtures[0]
        assert expect.columns == frozenset({"a", "b"})
        assert expect.model == "stg"

    def test_inline_csv_gives_its_header(self):
        index = build_unit_test_index(
            _manifest(
                _unit_test(
                    "unit_test.p.stg.t",
                    "stg",
                    expect={"format": "csv", "rows": "order_id, customer_id\n1,cust_abc"},
                )
            )
        )
        assert index["unit_test.p.stg.t"].fixtures[0].columns == frozenset(
            {"order_id", "customer_id"}
        )

    @pytest.mark.parametrize(
        "block,fragment",
        [
            ({"format": "csv", "fixture": "orders_seed"}, "fixture 'orders_seed'"),
            ({"format": "dict", "rows": []}, "no inline rows"),
            ({"format": "sql", "rows": "select 1 as id"}, "'sql' format"),
            ({"format": "dict", "rows": ["not a mapping"]}, "not name/value pairs"),
        ],
    )
    def test_unreadable_fixtures_say_why_rather_than_reading_as_clean(self, block, fragment):
        """Never None-and-silent: the caller has to be able to report the refusal."""
        index = build_unit_test_index(
            _manifest(_unit_test("unit_test.p.stg.t", "stg", expect=block))
        )
        expect = index["unit_test.p.stg.t"].fixtures[0]
        assert expect.columns is None
        assert fragment in expect.unreadable_reason

    def test_disabled_unit_tests_are_left_out(self):
        """dbt will not run them, so they cannot fail."""
        index = build_unit_test_index(
            _manifest(_unit_test("unit_test.p.stg.t", "stg", enabled=False))
        )
        assert index == {}

    @pytest.mark.parametrize(
        "expr,expected",
        [
            ("ref('stg_orders')", "stg_orders"),
            ('ref("stg_orders")', "stg_orders"),
            ("ref('a_package', 'stg_orders')", "stg_orders"),
            ("ref('stg_orders', v=2)", "stg_orders"),
            ("  ref( 'stg_orders' ) ", "stg_orders"),
            # Sources are outside dbt-plan's scope, and it does not diff them.
            ("source('raw', 'orders')", ""),
            ("", ""),
            ("this_is_not_a_call", ""),
        ],
    )
    def test_which_model_a_given_input_stands_for(self, expr, expected):
        assert _input_model(expr) == expected

    def test_given_inputs_become_fixtures_of_their_own_model(self):
        index = build_unit_test_index(
            _manifest(
                _unit_test(
                    "unit_test.p.dim.t",
                    "dim",
                    expect={"format": "dict", "rows": [{"title": "x"}]},
                    given=[
                        {
                            "input": "ref('stg_orders')",
                            "format": "dict",
                            "rows": [{"customer_id": "c"}],
                        },
                        {"input": "source('raw', 'orders')", "format": "dict", "rows": [{"a": 1}]},
                    ],
                )
            )
        )
        fixtures = index["unit_test.p.dim.t"].fixtures
        # expect belongs to dim; the ref() given belongs to stg_orders; the
        # source() given is dropped entirely.
        assert [(f.label, f.model) for f in fixtures] == [
            ("expect", "dim"),
            ("given for stg_orders", "stg_orders"),
        ]


class TestCascade:
    """The column drop below is the same one tests/dbt_project produces."""

    def _cascade(
        self,
        pred,
        *,
        child_map,
        unit_tests,
        columns=(["order_id", "customer_id"], ["order_id"]),
        downstream=(),
        node_index=None,
    ):
        # model_cols is what the CLI passes: the raw extraction. predict_ddl does
        # not populate columns_removed for a table or a view, so the column diff
        # for those comes from here.
        return analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={pred.model_name: f"model.p.{pred.model_name}"},
            model_cols={pred.model_name: (list(columns[0]), list(columns[1]))},
            all_downstream={f"model.p.{pred.model_name}": list(downstream)},
            node_index=node_index or {},
            base_node_index={},
            compiled_sql_index={},
            child_map=child_map,
            unit_test_index=build_unit_test_index(_manifest(*unit_tests)),
        )[0][0]

    def _view_dropping_customer_id(self):
        return predict_ddl(
            model_name="stg_orders",
            materialization="view",
            on_schema_change=None,
            base_columns=["order_id", "customer_id"],
            current_columns=["order_id"],
        )

    def test_expect_naming_a_dropped_column_turns_a_safe_view_into_a_warning(self):
        """The view itself is CREATE OR REPLACE and safe. `dbt build` still fails."""
        pred = self._view_dropping_customer_id()
        assert pred.safety == Safety.SAFE

        updated = self._cascade(
            pred,
            child_map={"model.p.stg_orders": ["unit_test.p.stg_orders.test_shape"]},
            unit_tests=[
                _unit_test(
                    "unit_test.p.stg_orders.test_shape",
                    "stg_orders",
                    expect={"format": "dict", "rows": [{"order_id": 1, "customer_id": "c"}]},
                )
            ],
        )
        assert updated.safety == Safety.WARNING
        impact = updated.downstream_impacts[0]
        assert impact.risk == "unit_test_failure"
        assert impact.model_name == "test_shape"
        assert "expect names dropped column(s): customer_id" in impact.reason

    def test_a_given_input_downstream_is_reached_through_the_model_it_tests(self):
        """child_map links a unit test only to its own model, never to that model's inputs."""
        pred = self._view_dropping_customer_id()
        updated = self._cascade(
            pred,
            downstream=["model.p.dim_books"],
            child_map={
                "model.p.stg_orders": ["model.p.dim_books"],
                "model.p.dim_books": ["unit_test.p.dim_books.test_groups"],
            },
            unit_tests=[
                _unit_test(
                    "unit_test.p.dim_books.test_groups",
                    "dim_books",
                    expect={"format": "dict", "rows": [{"title": "x"}]},
                    given=[
                        {
                            "input": "ref('stg_orders')",
                            "format": "dict",
                            "rows": [{"customer_id": "c"}],
                        }
                    ],
                )
            ],
        )
        impact = updated.downstream_impacts[0]
        assert impact.risk == "unit_test_failure"
        assert "given for stg_orders names dropped column(s): customer_id" in impact.reason

    def test_a_fixture_for_some_other_model_is_not_a_finding(self):
        """Same column name, different relation -- dbt checks each against its own."""
        pred = self._view_dropping_customer_id()
        updated = self._cascade(
            pred,
            downstream=["model.p.dim_books"],
            child_map={
                "model.p.stg_orders": ["model.p.dim_books"],
                "model.p.dim_books": ["unit_test.p.dim_books.test_groups"],
            },
            unit_tests=[
                _unit_test(
                    "unit_test.p.dim_books.test_groups",
                    "dim_books",
                    # dim_books keeps its own customer_id; nothing here is dropped.
                    expect={"format": "dict", "rows": [{"customer_id": "c"}]},
                    given=[
                        {
                            "input": "ref('other_source')",
                            "format": "dict",
                            "rows": [{"customer_id": "c"}],
                        }
                    ],
                )
            ],
        )
        assert updated.downstream_impacts == []
        assert updated.safety == Safety.SAFE

    def test_an_unreadable_fixture_is_reported_rather_than_passed_over(self):
        pred = self._view_dropping_customer_id()
        updated = self._cascade(
            pred,
            child_map={"model.p.stg_orders": ["unit_test.p.stg_orders.test_shape"]},
            unit_tests=[
                _unit_test(
                    "unit_test.p.stg_orders.test_shape",
                    "stg_orders",
                    expect={"format": "csv", "fixture": "expected_orders"},
                )
            ],
        )
        impact = updated.downstream_impacts[0]
        assert impact.risk == "unit_test_unreadable"
        assert "fixture 'expected_orders'" in impact.reason
        assert updated.safety == Safety.WARNING

    def test_a_column_only_added_leaves_unit_tests_alone(self):
        """An extra column in the model does not invalidate a fixture."""
        pred = predict_ddl(
            model_name="stg_orders",
            materialization="view",
            on_schema_change=None,
            base_columns=["order_id"],
            current_columns=["order_id", "shipped_at"],
        )
        updated = self._cascade(
            pred,
            columns=(["order_id"], ["order_id", "shipped_at"]),
            child_map={"model.p.stg_orders": ["unit_test.p.stg_orders.test_shape"]},
            unit_tests=[
                _unit_test(
                    "unit_test.p.stg_orders.test_shape",
                    "stg_orders",
                    expect={"format": "csv", "fixture": "expected_orders"},
                )
            ],
        )
        assert updated.downstream_impacts == []

    def test_incremental_ignore_still_checks_its_own_unit_tests(self):
        """No DDL runs, but a unit test executes the model's SELECT, not the merge."""
        pred = predict_ddl(
            model_name="stg_orders",
            materialization="incremental",
            on_schema_change="ignore",
            base_columns=["order_id", "customer_id"],
            current_columns=["order_id"],
        )
        assert pred.safety == Safety.SAFE

        updated = self._cascade(
            pred,
            downstream=["model.p.dim_books"],
            child_map={
                "model.p.stg_orders": ["model.p.dim_books", "unit_test.p.stg_orders.test_shape"],
                "model.p.dim_books": ["unit_test.p.dim_books.test_groups"],
            },
            unit_tests=[
                _unit_test(
                    "unit_test.p.stg_orders.test_shape",
                    "stg_orders",
                    expect={"format": "dict", "rows": [{"customer_id": "c"}]},
                ),
                _unit_test(
                    "unit_test.p.dim_books.test_groups",
                    "dim_books",
                    expect={"format": "dict", "rows": [{"title": "x"}]},
                    given=[
                        {
                            "input": "ref('stg_orders')",
                            "format": "dict",
                            "rows": [{"customer_id": "c"}],
                        }
                    ],
                ),
            ],
        )
        # Its own test breaks. Nothing downstream moves, because the physical
        # table keeps the column.
        assert [i.model_name for i in updated.downstream_impacts] == ["test_shape"]
        assert updated.safety == Safety.WARNING

    def test_a_unit_test_finding_never_downgrades_a_destructive_verdict(self):
        pred = predict_ddl(
            model_name="stg_orders",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["order_id", "customer_id"],
            current_columns=["order_id"],
        )
        assert pred.safety == Safety.DESTRUCTIVE

        updated = self._cascade(
            pred,
            child_map={"model.p.stg_orders": ["unit_test.p.stg_orders.test_shape"]},
            unit_tests=[
                _unit_test(
                    "unit_test.p.stg_orders.test_shape",
                    "stg_orders",
                    expect={"format": "dict", "rows": [{"customer_id": "c"}]},
                )
            ],
        )
        assert updated.downstream_impacts[0].risk == "unit_test_failure"
        assert updated.safety == Safety.DESTRUCTIVE

    def test_without_a_unit_test_index_nothing_changes(self):
        """The parameters are optional, so an older caller keeps working."""
        pred = self._view_dropping_customer_id()
        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"stg_orders": "model.p.stg_orders"},
            model_cols={},
            all_downstream={"model.p.stg_orders": []},
            node_index={},
            base_node_index={},
            compiled_sql_index={},
        )
        assert updated[0] == pred


class TestCompiledUnitTestSqlIsNotAModel:
    """dbt compiles unit tests into target/, under the schema file that declared them.

        target/compiled/p/models/schema.yml/models/test_orders_shape.sql

    Diffed as a model, that file is reported as "not found in manifest" on every
    run, and two models that each carry a `test_shape` abort the run outright on
    the duplicate-stem check.
    """

    def _tree(self, root, *relative_paths):
        for rel in relative_paths:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SELECT 1", encoding="utf-8")
        return root

    def test_yaml_defined_nodes_are_skipped(self, tmp_path):
        from dbt_plan.diff import iter_model_sql

        root = self._tree(
            tmp_path,
            "staging/stg_orders.sql",
            "schema.yml/models/test_stg_orders_shape.sql",
            "marts/schema.yaml/models/test_dim_books.sql",
        )
        assert [p.name for p in iter_model_sql(root)] == ["stg_orders.sql"]

    def test_two_models_with_a_same_named_unit_test_do_not_collide(self, tmp_path):
        """Unit test names are unique per model, not per project."""
        from dbt_plan.diff import diff_compiled_dirs

        base = self._tree(
            tmp_path / "base",
            "a.sql",
            "staging/schema.yml/models/test_shape.sql",
            "marts/schema.yml/models/test_shape.sql",
        )
        current = self._tree(
            tmp_path / "current",
            "a.sql",
            "staging/schema.yml/models/test_shape.sql",
            "marts/schema.yml/models/test_shape.sql",
        )
        assert diff_compiled_dirs(base, current) == []

    def test_a_uncompiled_model_is_not_masked_by_a_same_named_unit_test(self, tmp_path):
        """The compiled-stem set is a false-safe guard; a unit test must not fill it."""
        from dbt_plan.diff import iter_model_sql

        root = self._tree(tmp_path, "schema.yml/models/stg_orders.sql")
        assert {f.stem for f in iter_model_sql(root)} == set()

    def test_a_model_directory_ending_in_sql_is_still_scanned(self, tmp_path):
        """Only `.yml`/`.yaml` segments are dbt's marker; nothing else is filtered."""
        from dbt_plan.diff import iter_model_sql

        root = self._tree(tmp_path, "yml/stg_orders.sql", "schema.yml.sql")
        assert sorted(p.name for p in iter_model_sql(root)) == ["schema.yml.sql", "stg_orders.sql"]


def test_prediction_is_unchanged_when_a_model_has_no_unit_tests():
    """Regression guard: the whole feature is inert on a project without any."""
    pred = predict_ddl(
        model_name="stg_orders",
        materialization="view",
        on_schema_change=None,
        base_columns=["order_id", "customer_id"],
        current_columns=["order_id"],
    )
    updated, _ = analyze_cascade_impacts(
        predictions=[pred],
        model_node_ids={"stg_orders": "model.p.stg_orders"},
        model_cols={},
        all_downstream={"model.p.stg_orders": []},
        node_index={},
        base_node_index={},
        compiled_sql_index={},
        child_map={"model.p.stg_orders": []},
        unit_test_index={},
    )
    assert isinstance(updated[0], DDLPrediction)
    assert updated[0].safety == Safety.SAFE
