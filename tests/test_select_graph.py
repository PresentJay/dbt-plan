"""`--select` with dbt's graph operators.

It used to be an exact set-membership test, so anyone reaching for a flag spelled
`--select` on a dbt tool got the one meaning dbt gives it that dbt-plan supported.
`fct_orders+` matched nothing and the run reported "no changed models" for a
filter the author believed was broader.

Only the three operators dbt-plan already holds the graph for are supported.
`tag:` and `path:` are reported rather than silently matching nothing, because a
`--select` narrower than intended hides findings.
"""

from __future__ import annotations

import pytest

from dbt_plan.cli import _expand_selection
from dbt_plan.manifest import build_node_index

# stg_orders -> dim_books -> rpt_top_books, and stg_orders -> fct_orders
_CHILD_MAP = {
    "model.p.stg_orders": ["model.p.dim_books", "model.p.fct_orders", "test.p.not_null.abc"],
    "model.p.dim_books": ["model.p.rpt_top_books"],
    "model.p.fct_orders": [],
    "model.p.rpt_top_books": [],
}
_NODE_INDEX = build_node_index(
    {
        "metadata": {"project_name": "p"},
        "nodes": {
            f"model.p.{name}": {"name": name, "path": f"{name}.sql", "config": {}}
            for name in ("stg_orders", "dim_books", "fct_orders", "rpt_top_books")
        },
    }
)


def _select(terms):
    return _expand_selection(terms, _CHILD_MAP, _NODE_INDEX)


class TestGraphOperators:
    def test_a_bare_name_is_that_model_alone(self):
        assert _select("dim_books") == ({"dim_books"}, [])

    def test_trailing_plus_takes_everything_downstream(self):
        """The one that earns its keep: this model and what it breaks."""
        selected, _ = _select("stg_orders+")
        assert selected == {"stg_orders", "dim_books", "fct_orders", "rpt_top_books"}

    def test_leading_plus_takes_everything_upstream(self):
        selected, _ = _select("+rpt_top_books")
        assert selected == {"rpt_top_books", "dim_books", "stg_orders"}

    def test_both_ends_take_the_whole_line_through_the_model(self):
        selected, _ = _select("+dim_books+")
        assert selected == {"dim_books", "stg_orders", "rpt_top_books"}

    def test_non_model_children_are_not_selected(self):
        """A test node is downstream, but `--select` filters the changed *models*."""
        selected, _ = _select("stg_orders+")
        assert not any(name.startswith("not_null") for name in selected)

    def test_terms_are_unioned(self):
        selected, _ = _select("fct_orders, +dim_books")
        assert selected == {"fct_orders", "dim_books", "stg_orders"}

    def test_a_leaf_with_a_plus_is_just_itself(self):
        assert _select("rpt_top_books+") == ({"rpt_top_books"}, [])

    def test_a_name_not_in_the_manifest_still_selects_itself(self):
        """Same as before: it matches nothing and the caller warns about that."""
        assert _select("typo_model+") == ({"typo_model"}, [])

    @pytest.mark.parametrize("term", ["", "  ", "+", "++"])
    def test_empty_terms_are_skipped(self, term):
        assert _select(term) == (set(), [])


class TestUnsupportedSelectors:
    @pytest.mark.parametrize("term", ["tag:nightly", "path:models/staging", "staging.*"])
    def test_they_are_reported_rather_than_matching_nothing(self, term):
        """Silently matching nothing is how a filter hides findings."""
        selected, unsupported = _select(term)
        assert selected == set()
        assert unsupported == [term]

    def test_the_supported_half_of_a_mixed_filter_still_runs(self):
        selected, unsupported = _select("dim_books, tag:nightly")
        assert selected == {"dim_books"}
        assert unsupported == ["tag:nightly"]
