"""Resolving a downstream reference instead of searching for its name.

Cascade detection was textual: look for the dropped column's name anywhere in the
downstream compiled SQL. `docs/use-cases.md` apologised for it -- "a column named
`id` will match a great deal" -- and the apology was earned:

    SELECT o.order_id, c.customer_id, c.tier
    FROM stg_orders o JOIN dim_customers c ON o.order_id = 1
    WHERE 'customer_id' <> ''
    -- customer_id used to live here

Nothing there reads `stg_orders.customer_id`. dbt-plan 0.13.0 reported it as a
broken ref, three times over: the comment, the string literal, and the other
table's column of the same name.

dbt-plan has a schema -- every model's columns, from the project's own compiled
SQL -- so the reference can be resolved rather than matched. The text search stays
as the fallback, because a refusal has to widen what gets reported, never narrow it.
"""

from __future__ import annotations

import pytest

from dbt_plan.columns import columns_read_from

SCHEMA = {
    "stg_orders": {"order_id": "INT", "customer_id": "VARCHAR", "status": "VARCHAR"},
    "dim_customers": {"customer_id": "VARCHAR", "tier": "VARCHAR"},
}


def _read(sql, relation="stg_orders"):
    return columns_read_from(sql, relation, SCHEMA, dialect="duckdb")


class TestWhatItReads:
    def test_a_column_it_names(self):
        assert _read("SELECT customer_id FROM stg_orders") == ["customer_id"]

    def test_a_fully_qualified_relation_matches_the_bare_schema_key(self):
        """Compiled dbt SQL never names the model; it names the relation it writes."""
        assert _read('SELECT customer_id FROM "dev"."main"."stg_orders"') == ["customer_id"]

    def test_a_name_in_a_comment_or_a_string_is_not_a_reference(self):
        sql = "SELECT order_id FROM stg_orders WHERE 'customer_id' <> '' -- customer_id"
        assert _read(sql) == ["order_id"]

    def test_the_same_column_name_on_another_table_is_not_a_reference(self):
        sql = "SELECT c.customer_id FROM stg_orders o JOIN dim_customers c ON o.order_id = 1"
        assert _read(sql) == ["order_id"]

    def test_a_relation_it_does_not_read_at_all(self):
        assert _read("SELECT tier FROM dim_customers") == []

    def test_an_alias_is_followed(self):
        assert _read("SELECT o.status FROM stg_orders AS o") == ["status"]


class TestStarsAreNotBreakage:
    """`select *` returns one column fewer; it does not fail.

    That loss is a real finding and predict_ddl already makes it, for the model
    that inherits it. Counting it here as well would report the same change twice,
    once with the wrong severity.
    """

    def test_a_bare_star_reads_nothing_by_name(self):
        assert _read("SELECT * FROM stg_orders") == []

    def test_a_qualified_star_reads_nothing_by_name(self):
        assert _read("SELECT o.* FROM stg_orders o") == []

    def test_a_star_alongside_a_named_column_still_reports_the_named_one(self):
        assert _read("SELECT *, customer_id FROM stg_orders") == ["customer_id"]

    def test_count_star_is_not_a_projection_star(self):
        assert _read("SELECT count(*) AS n, customer_id FROM stg_orders GROUP BY 1") == [
            "customer_id"
        ]


class TestItRefusesRatherThanGuesses:
    """None means "fall back to the text search", which is the wider net."""

    @pytest.mark.parametrize(
        "sql,why",
        [
            ("SELECT FROM WHERE (((", "will not parse"),
            ("WITH s AS (SELECT * FROM raw.unknown) SELECT customer_id FROM s", "unattributable"),
            ("SELECT no_such_column FROM stg_orders", "not in the schema"),
        ],
    )
    def test_it_returns_none(self, sql, why):
        assert _read(sql) is None, why

    def test_one_table_and_no_schema_is_inferred_rather_than_refused(self):
        """With a single relation in the query the attribution is not a guess."""
        assert columns_read_from(
            "SELECT customer_id FROM stg_orders", "stg_orders", {}, dialect="duckdb"
        ) == ["customer_id"]


class TestTheReaderOnlyAnswersWhenItHasTheSchema:
    """The guard that matters: without the changed model's columns, a bare column
    reference could be attributed to the wrong relation, and `[]` would read as
    "does not use it" -- a false all-clear rather than a false warning."""

    def _reader(self, tmp_path, known):
        from dbt_plan.cli import _make_reference_reader

        sql = tmp_path / "fct_orders.sql"
        sql.write_text("SELECT customer_id FROM stg_orders", encoding="utf-8")
        columns = {"stg_orders": ["order_id", "customer_id"]}
        return _make_reference_reader(
            {"fct_orders": sql},
            dict.fromkeys(known),
            lambda name: columns.get(name),
            "duckdb",
        )

    def test_it_answers_when_the_changed_model_is_known(self, tmp_path):
        assert self._reader(tmp_path, ["stg_orders"])("fct_orders", "stg_orders") == [
            "customer_id"
        ]

    def test_it_refuses_when_the_changed_model_is_not(self, tmp_path):
        assert self._reader(tmp_path, ["something_else"])("fct_orders", "stg_orders") is None

    def test_it_refuses_when_the_downstream_sql_is_not_on_disk(self, tmp_path):
        assert self._reader(tmp_path, ["stg_orders"])("no_such_model", "stg_orders") is None


class TestThroughTheCascade:
    def _cascade(self, *, reads):
        from dbt_plan.manifest import ModelNode
        from dbt_plan.predictor import Safety, analyze_cascade_impacts, predict_ddl

        pred = predict_ddl(
            model_name="stg_orders",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["order_id", "customer_id"],
            current_columns=["order_id"],
        )
        assert pred.safety == Safety.DESTRUCTIVE
        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"stg_orders": "model.p.stg_orders"},
            model_cols={"stg_orders": (["order_id", "customer_id"], ["order_id"])},
            all_downstream={"model.p.stg_orders": ["model.p.fct_orders"]},
            node_index={
                "fct_orders": ModelNode(
                    node_id="model.p.fct_orders",
                    name="fct_orders",
                    materialization="table",
                    on_schema_change=None,
                )
            },
            base_node_index={},
            compiled_sql_index={},
            columns_read_of=lambda downstream, changed: reads,
        )
        return updated[0].downstream_impacts

    def test_a_resolved_read_is_reported(self):
        impacts = self._cascade(reads=["customer_id"])
        assert [(i.risk, i.reason) for i in impacts] == [
            ("broken_ref", "reads dropped column(s): customer_id")
        ]

    def test_a_resolved_non_read_is_not(self):
        """The false warning this exists to remove."""
        assert self._cascade(reads=["order_id"]) == []

    def test_a_refusal_falls_back_to_the_text_search(self, tmp_path):
        """Not to silence. The wider net has to catch what the precise one dropped."""
        from dbt_plan.manifest import ModelNode
        from dbt_plan.predictor import analyze_cascade_impacts, predict_ddl

        sql = tmp_path / "fct_orders.sql"
        sql.write_text("SELECT customer_id FROM stg_orders", encoding="utf-8")

        pred = predict_ddl(
            model_name="stg_orders",
            materialization="incremental",
            on_schema_change="sync_all_columns",
            base_columns=["order_id", "customer_id"],
            current_columns=["order_id"],
        )
        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"stg_orders": "model.p.stg_orders"},
            model_cols={"stg_orders": (["order_id", "customer_id"], ["order_id"])},
            all_downstream={"model.p.stg_orders": ["model.p.fct_orders"]},
            node_index={
                "fct_orders": ModelNode(
                    node_id="model.p.fct_orders",
                    name="fct_orders",
                    materialization="table",
                    on_schema_change=None,
                )
            },
            base_node_index={},
            compiled_sql_index={"fct_orders": sql},
            columns_read_of=lambda downstream, changed: None,
        )
        impacts = updated[0].downstream_impacts
        assert [(i.risk, i.reason) for i in impacts] == [
            ("broken_ref", "references dropped column(s): customer_id")
        ]


class TestReadsAreCheckedAgainstEveryModelLosingAColumn:
    """#121 resolved the reference and asked about one relation: the model that changed.

    The text search it replaced matched the column's name anywhere, so it caught a
    read two hops away by accident:

        stg_orders:  SELECT order_id, customer_id  ->  SELECT order_id
        mid:         SELECT * FROM stg_orders               (never mentions customer_id)
        fct_twohop:  SELECT customer_id FROM mid            (never mentions stg_orders)

    `fct_twohop` reads `customer_id` from `mid`, and `mid` is losing it. Asked only
    about `stg_orders`, the resolver said `[]` -- does not read that relation -- which
    is true and beside the point, and it suppressed the fallback that used to fire.
    Measured: dbt build fails `fct_twohop`; 0.14.0 reported nothing about it.
    """

    def _cascade(self, reads_by_pair, *, downstream_sql=None):
        from dbt_plan.manifest import ModelNode
        from dbt_plan.predictor import analyze_cascade_impacts, predict_ddl

        pred = predict_ddl(
            model_name="stg_orders",
            materialization="view",
            on_schema_change=None,
            base_columns=["order_id", "customer_id"],
            current_columns=["order_id"],
        )
        node = lambda n, mat="table": ModelNode(  # noqa: E731
            node_id=f"model.p.{n}", name=n, materialization=mat, on_schema_change=None
        )
        base_cols = {"mid": ["order_id", "customer_id"], "fct_twohop": ["customer_id"]}
        curr_cols = {"mid": ["order_id"], "fct_twohop": ["customer_id"]}
        updated, _ = analyze_cascade_impacts(
            predictions=[pred],
            model_node_ids={"stg_orders": "model.p.stg_orders"},
            model_cols={"stg_orders": (["order_id", "customer_id"], ["order_id"])},
            all_downstream={"model.p.stg_orders": ["model.p.mid", "model.p.fct_twohop"]},
            node_index={"mid": node("mid", "view"), "fct_twohop": node("fct_twohop")},
            base_node_index={},
            compiled_sql_index=downstream_sql or {},
            base_columns_of=base_cols.get,
            current_columns_of=curr_cols.get,
            columns_read_of=lambda d, m: reads_by_pair.get((d, m)),
        )
        return [(i.model_name, i.risk, i.reason) for i in updated[0].downstream_impacts]

    def test_a_read_from_the_passthrough_is_a_broken_ref(self):
        impacts = self._cascade(
            {
                ("fct_twohop", "stg_orders"): [],  # true: it never mentions stg_orders
                ("fct_twohop", "mid"): ["customer_id"],  # and this is where it breaks
                ("mid", "stg_orders"): [],  # a star names nothing
            }
        )
        assert ("fct_twohop", "broken_ref", "reads dropped column(s): customer_id") in impacts

    def test_the_passthrough_itself_is_not_a_broken_ref(self):
        """`mid` selects `*`; it loses the column without failing. That is #24's finding."""
        impacts = self._cascade(
            {
                ("fct_twohop", "stg_orders"): [],
                ("fct_twohop", "mid"): [],
                ("mid", "stg_orders"): [],
            }
        )
        assert not any(name == "mid" and risk == "broken_ref" for name, risk, _ in impacts)

    def test_a_refusal_about_the_passthrough_falls_back_to_the_text(self, tmp_path):
        sql = tmp_path / "fct_twohop.sql"
        sql.write_text("SELECT customer_id FROM mid", encoding="utf-8")
        impacts = self._cascade(
            {("fct_twohop", "stg_orders"): [], ("fct_twohop", "mid"): None},
            downstream_sql={"fct_twohop": sql},
        )
        assert ("fct_twohop", "broken_ref", "references dropped column(s): customer_id") in (
            impacts
        )


class TestAModelIsKnownByItsRelationToo:
    """`config(alias='orders_clean')` makes stg_orders write to orders_clean, and the
    compiled SQL of anything reading it names orders_clean. Matching on the model
    name alone finds no such table and answers "does not read it"."""

    def test_the_relation_name_resolves_the_read(self):
        schema = {"orders_clean": {"order_id": "INT", "customer_id": "VARCHAR"}}
        assert columns_read_from(
            'SELECT customer_id FROM "db"."main"."orders_clean"',
            {"stg_orders", "orders_clean"},
            schema,
            dialect="duckdb",
        ) == ["customer_id"]

    def test_the_reader_learns_the_alias_from_the_relation_index(self, tmp_path):
        from dbt_plan.cli import _make_reference_reader

        sql = tmp_path / "fct.sql"
        sql.write_text('SELECT customer_id FROM "db"."main"."orders_clean"', encoding="utf-8")
        columns = {"stg_orders": ["order_id", "customer_id"]}
        read = _make_reference_reader(
            {"fct": sql},
            {"stg_orders": None},
            lambda name: columns.get(name),
            "duckdb",
            {"db.main.orders_clean": "stg_orders", "stg_orders": "stg_orders"},
        )
        assert read("fct", "stg_orders") == ["customer_id"]


class TestOneParsePerDownstreamModel:
    """Re-qualifying the same SQL once per lost model put a 200-model chain at 38s."""

    def test_the_downstream_sql_is_parsed_once_however_many_models_are_asked_about(
        self, tmp_path, monkeypatch
    ):
        import dbt_plan.columns as columns_mod
        from dbt_plan.cli import _make_reference_reader

        sql = tmp_path / "fct.sql"
        sql.write_text("SELECT a FROM m_1", encoding="utf-8")
        calls = []
        original = columns_mod.columns_read_by_relation

        def counting(*a, **kw):
            calls.append(1)
            return original(*a, **kw)

        monkeypatch.setattr(columns_mod, "columns_read_by_relation", counting)
        cols = {f"m_{i}": ["a"] for i in range(50)}
        read = _make_reference_reader({"fct": sql}, dict.fromkeys(cols), cols.get, "duckdb")
        for i in range(50):
            read("fct", f"m_{i}")
        assert len(calls) == 1
