"""E-commerce dbt project integration test — realistic 15-model project simulation.

Simulates a junior analytics engineer running dbt-plan against an e-commerce
dbt project with staging views, intermediate incremental models, and mart tables.
Each scenario tests a specific change pattern through the full _do_check pipeline
with JSON output, asserting on safety levels, operations, and cascade impacts.
"""

from __future__ import annotations

import argparse
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# SQL fixtures — realistic compiled SQL for each model
# ---------------------------------------------------------------------------

STG_ORDERS = """\
SELECT
    order_id,
    customer_id,
    order_date,
    status,
    total_amount
FROM raw.orders
"""

STG_CUSTOMERS = """\
SELECT
    customer_id,
    name,
    email,
    created_at
FROM raw.customers
"""

STG_PRODUCTS = """\
SELECT
    product_id,
    name,
    price,
    category
FROM raw.products
"""

STG_ORDER_ITEMS = """\
SELECT
    order_item_id,
    order_id,
    product_id,
    quantity,
    unit_price
FROM raw.order_items
"""

INT_ORDER_ENRICHED = """\
WITH orders AS (
    SELECT * FROM stg_orders
),
customers AS (
    SELECT * FROM stg_customers
),
order_items AS (
    SELECT * FROM stg_order_items
),
item_counts AS (
    SELECT
        order_id,
        COUNT(*) AS item_count
    FROM order_items
    GROUP BY order_id
)
SELECT
    o.order_id,
    o.customer_id,
    c.name AS customer_name,
    o.order_date,
    o.total_amount,
    ic.item_count
FROM orders o
LEFT JOIN customers c ON o.customer_id = c.customer_id
LEFT JOIN item_counts ic ON o.order_id = ic.order_id
"""

INT_CUSTOMER_METRICS = """\
WITH enriched AS (
    SELECT * FROM int_order_enriched
)
SELECT
    customer_id,
    customer_name,
    COUNT(*) AS total_orders,
    SUM(total_amount) AS total_sales,
    MIN(order_date) AS first_order_date,
    MAX(order_date) AS last_order_date
FROM enriched
GROUP BY customer_id, customer_name
"""

FCT_DAILY_REVENUE = """\
WITH enriched AS (
    SELECT * FROM int_order_enriched
)
SELECT
    order_date,
    SUM(total_amount) AS total_sales,
    COUNT(DISTINCT order_id) AS order_count,
    COUNT(DISTINCT customer_id) AS unique_customers
FROM enriched
GROUP BY order_date
"""

FCT_CUSTOMER_LTV = """\
WITH metrics AS (
    SELECT * FROM int_customer_metrics
)
SELECT
    customer_id,
    customer_name,
    total_sales AS ltv,
    total_orders AS order_count,
    DATEDIFF(day, first_order_date, CURRENT_DATE()) AS days_since_first_order
FROM metrics
"""

DIM_PRODUCTS = """\
SELECT
    product_id,
    name,
    price,
    category
FROM stg_products
"""

# Additional models to reach 15 total
DIM_CUSTOMERS = """\
WITH metrics AS (
    SELECT * FROM int_customer_metrics
)
SELECT
    customer_id,
    customer_name,
    total_orders,
    total_sales,
    first_order_date,
    last_order_date
FROM metrics
"""

FCT_ORDER_ITEMS = """\
WITH items AS (
    SELECT * FROM stg_order_items
),
products AS (
    SELECT * FROM stg_products
)
SELECT
    i.order_item_id,
    i.order_id,
    i.product_id,
    p.name AS product_name,
    i.quantity,
    i.unit_price,
    i.quantity * i.unit_price AS line_total
FROM items i
LEFT JOIN products p ON i.product_id = p.product_id
"""

RPT_TOP_PRODUCTS = """\
WITH order_items AS (
    SELECT * FROM fct_order_items
)
SELECT
    product_id,
    product_name,
    SUM(quantity) AS total_quantity,
    SUM(line_total) AS total_sales
FROM order_items
GROUP BY product_id, product_name
"""

RPT_MONTHLY_REVENUE = """\
WITH daily AS (
    SELECT * FROM fct_daily_revenue
)
SELECT
    DATE_TRUNC('month', order_date) AS month,
    SUM(total_sales) AS monthly_revenue,
    SUM(order_count) AS monthly_orders,
    SUM(unique_customers) AS monthly_unique_customers
FROM daily
GROUP BY DATE_TRUNC('month', order_date)
"""

STG_PAYMENTS = """\
SELECT
    payment_id,
    order_id,
    payment_method,
    amount,
    payment_date
FROM raw.payments
"""

INT_ORDER_PAYMENTS = """\
WITH orders AS (
    SELECT * FROM stg_orders
),
payments AS (
    SELECT * FROM stg_payments
)
SELECT
    o.order_id,
    o.customer_id,
    o.total_amount AS order_total,
    p.payment_method,
    p.amount AS payment_amount,
    p.payment_date
FROM orders o
LEFT JOIN payments p ON o.order_id = p.order_id
"""


# ---------------------------------------------------------------------------
# All model SQL and configs
# ---------------------------------------------------------------------------

ALL_SQLS = {
    "stg_orders": STG_ORDERS,
    "stg_customers": STG_CUSTOMERS,
    "stg_products": STG_PRODUCTS,
    "stg_order_items": STG_ORDER_ITEMS,
    "stg_payments": STG_PAYMENTS,
    "int_order_enriched": INT_ORDER_ENRICHED,
    "int_customer_metrics": INT_CUSTOMER_METRICS,
    "int_order_payments": INT_ORDER_PAYMENTS,
    "fct_daily_revenue": FCT_DAILY_REVENUE,
    "fct_customer_ltv": FCT_CUSTOMER_LTV,
    "fct_order_items": FCT_ORDER_ITEMS,
    "dim_products": DIM_PRODUCTS,
    "dim_customers": DIM_CUSTOMERS,
    "rpt_top_products": RPT_TOP_PRODUCTS,
    "rpt_monthly_revenue": RPT_MONTHLY_REVENUE,
}

MODEL_CONFIGS = {
    # Staging: views
    "stg_orders": {"materialization": "view"},
    "stg_customers": {"materialization": "view"},
    "stg_products": {"materialization": "view"},
    "stg_order_items": {"materialization": "view"},
    "stg_payments": {"materialization": "view"},
    # Intermediate: incremental, sync_all_columns
    "int_order_enriched": {
        "materialization": "incremental",
        "on_schema_change": "sync_all_columns",
    },
    "int_customer_metrics": {
        "materialization": "incremental",
        "on_schema_change": "sync_all_columns",
    },
    "int_order_payments": {
        "materialization": "incremental",
        "on_schema_change": "sync_all_columns",
    },
    # Marts: incremental, fail
    "fct_daily_revenue": {
        "materialization": "incremental",
        "on_schema_change": "fail",
    },
    "fct_customer_ltv": {
        "materialization": "incremental",
        "on_schema_change": "fail",
    },
    "fct_order_items": {
        "materialization": "incremental",
        "on_schema_change": "fail",
    },
    # Marts: table
    "dim_products": {"materialization": "table"},
    "dim_customers": {"materialization": "table"},
    # Reports: table
    "rpt_top_products": {"materialization": "table"},
    "rpt_monthly_revenue": {"materialization": "table"},
}

# DAG — child_map defines downstream dependencies
CHILD_MAP = {
    "model.ecommerce.stg_orders": [
        "model.ecommerce.int_order_enriched",
        "model.ecommerce.int_order_payments",
    ],
    "model.ecommerce.stg_customers": [
        "model.ecommerce.int_order_enriched",
    ],
    "model.ecommerce.stg_order_items": [
        "model.ecommerce.int_order_enriched",
        "model.ecommerce.fct_order_items",
    ],
    "model.ecommerce.stg_products": [
        "model.ecommerce.dim_products",
        "model.ecommerce.fct_order_items",
    ],
    "model.ecommerce.stg_payments": [
        "model.ecommerce.int_order_payments",
    ],
    "model.ecommerce.int_order_enriched": [
        "model.ecommerce.int_customer_metrics",
        "model.ecommerce.fct_daily_revenue",
    ],
    "model.ecommerce.int_customer_metrics": [
        "model.ecommerce.fct_customer_ltv",
        "model.ecommerce.dim_customers",
    ],
    "model.ecommerce.int_order_payments": [],
    "model.ecommerce.fct_daily_revenue": [
        "model.ecommerce.rpt_monthly_revenue",
    ],
    "model.ecommerce.fct_customer_ltv": [],
    "model.ecommerce.fct_order_items": [
        "model.ecommerce.rpt_top_products",
    ],
    "model.ecommerce.dim_products": [],
    "model.ecommerce.dim_customers": [],
    "model.ecommerce.rpt_top_products": [],
    "model.ecommerce.rpt_monthly_revenue": [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_manifest(
    node_overrides: dict | None = None,
    child_map_overrides: dict | None = None,
) -> dict:
    """Build a full e-commerce manifest.json.

    Args:
        node_overrides: Per-model config overrides (e.g., change materialization).
        child_map_overrides: Override specific child_map entries.
    """
    configs = {name: dict(cfg) for name, cfg in MODEL_CONFIGS.items()}
    if node_overrides:
        for name, overrides in node_overrides.items():
            if name in configs:
                configs[name].update(overrides)
            else:
                configs[name] = overrides

    nodes = {}
    for name, cfg in configs.items():
        node_id = f"model.ecommerce.{name}"
        node = {
            "name": name,
            "config": {
                "materialized": cfg["materialization"],
            },
            "columns": {},
        }
        if "on_schema_change" in cfg:
            node["config"]["on_schema_change"] = cfg["on_schema_change"]
        nodes[node_id] = node

    cmap = dict(CHILD_MAP)
    if child_map_overrides:
        cmap.update(child_map_overrides)

    return {
        "nodes": nodes,
        "child_map": cmap,
        "metadata": {"project_name": "ecommerce"},
    }


def _setup_project(
    tmp_path: Path,
    *,
    base_sqls: dict[str, str],
    current_sqls: dict[str, str],
    manifest: dict,
    base_manifest: dict | None = None,
) -> Path:
    """Create a full e-commerce project directory for _do_check."""
    project_dir = tmp_path / "ecommerce"
    project_dir.mkdir()

    # Current compiled SQL
    compiled_dir = project_dir / "target" / "compiled" / "ecommerce" / "models"
    compiled_dir.mkdir(parents=True)
    for name, sql in current_sqls.items():
        (compiled_dir / f"{name}.sql").write_text(sql)

    # manifest.json
    (project_dir / "target" / "manifest.json").write_text(json.dumps(manifest))

    # Base snapshot
    base_compiled = project_dir / ".dbt-plan" / "base" / "compiled"
    base_compiled.mkdir(parents=True)
    for name, sql in base_sqls.items():
        (base_compiled / f"{name}.sql").write_text(sql)

    # Base manifest
    base_manifest_data = base_manifest if base_manifest is not None else manifest
    (project_dir / ".dbt-plan" / "base" / "manifest.json").write_text(
        json.dumps(base_manifest_data)
    )

    return project_dir


def _run_check(
    project_dir: Path,
    *,
    fmt: str = "json",
) -> tuple[int, dict | str]:
    """Run _do_check and capture stdout."""
    from dbt_plan.cli import _do_check

    args = argparse.Namespace(
        project_dir=str(project_dir),
        target_dir="target",
        base_dir=".dbt-plan/base",
        manifest=None,
        format=fmt,
        no_color=True,
        select=None,
        verbose=False,
        dialect=None,
    )
    buf = StringIO()
    with patch("sys.stdout", buf):
        exit_code = _do_check(args)
    output = buf.getvalue()
    if fmt == "json":
        return exit_code, json.loads(output)
    return exit_code, output


def _find_model(result: dict, model_name: str) -> dict | None:
    """Find a model in the JSON result by name."""
    for m in result["models"]:
        if m["model_name"] == model_name:
            return m
    return None


# ---------------------------------------------------------------------------
# Scenario A — Remove a customer field (cascade impact)
# ---------------------------------------------------------------------------


class TestScenarioA_RemoveCustomerField:
    """Remove customer_name from int_order_enriched.

    Expected:
    - int_order_enriched: DESTRUCTIVE (sync_all_columns + DROP COLUMN customer_name)
    - cascade: fct_customer_ltv references customer_name (via int_customer_metrics)
    - cascade: fct_daily_revenue NOT flagged (doesn't reference customer_name)
    """

    def test_remove_customer_name(self, tmp_path: Path):
        # Modified int_order_enriched: customer_name removed
        modified_int_order_enriched = """\
WITH orders AS (
    SELECT * FROM stg_orders
),
customers AS (
    SELECT * FROM stg_customers
),
order_items AS (
    SELECT * FROM stg_order_items
),
item_counts AS (
    SELECT
        order_id,
        COUNT(*) AS item_count
    FROM order_items
    GROUP BY order_id
)
SELECT
    o.order_id,
    o.customer_id,
    o.order_date,
    o.total_amount,
    ic.item_count
FROM orders o
LEFT JOIN customers c ON o.customer_id = c.customer_id
LEFT JOIN item_counts ic ON o.order_id = ic.order_id
"""
        # Build SQL dictionaries — only int_order_enriched changes
        base_sqls = dict(ALL_SQLS)
        current_sqls = dict(ALL_SQLS)
        current_sqls["int_order_enriched"] = modified_int_order_enriched

        manifest = _build_manifest()
        project_dir = _setup_project(
            tmp_path,
            base_sqls=base_sqls,
            current_sqls=current_sqls,
            manifest=manifest,
        )

        exit_code, result = _run_check(project_dir)

        # 1. int_order_enriched should be DESTRUCTIVE
        ioe = _find_model(result, "int_order_enriched")
        assert ioe is not None, "int_order_enriched should appear in results"
        assert ioe["safety"] == "destructive"
        assert "customer_name" in ioe["columns_removed"]

        # Operations should include DROP COLUMN customer_name
        ops = [op["operation"] for op in ioe["operations"]]
        assert any("DROP COLUMN" in op for op in ops)
        drop_ops = [op for op in ioe["operations"] if op["operation"] == "DROP COLUMN"]
        assert any(op["column"] == "customer_name" for op in drop_ops)

        # 2. Cascade: downstream models referencing customer_name should be flagged
        assert "downstream_impacts" in ioe
        impacts = ioe["downstream_impacts"]

        # int_customer_metrics references customer_name directly in its SQL
        broken_refs = [i for i in impacts if i["risk"] == "broken_ref"]
        broken_model_names = [i["model_name"] for i in broken_refs]
        assert "int_customer_metrics" in broken_model_names, (
            "int_customer_metrics should be flagged for broken_ref on customer_name"
        )

        # 3. fct_daily_revenue should NOT be in the output as a changed model
        #    (its SQL didn't change) and should not have a broken_ref
        #    because it doesn't reference customer_name
        fdr = _find_model(result, "fct_daily_revenue")
        assert fdr is None, "fct_daily_revenue should NOT appear as a changed model"

        # If fct_daily_revenue appears in cascade impacts, it should not be broken_ref
        fdr_impacts = [i for i in impacts if i["model_name"] == "fct_daily_revenue"]
        fdr_broken = [i for i in fdr_impacts if i["risk"] == "broken_ref"]
        assert len(fdr_broken) == 0, (
            "fct_daily_revenue should NOT have broken_ref (doesn't reference customer_name)"
        )

        # 4. Exit code should be 1 (destructive)
        assert exit_code == 1


# ---------------------------------------------------------------------------
# Scenario B — Add a new metric to fct_daily_revenue
# ---------------------------------------------------------------------------


class TestScenarioB_AddNewMetric:
    """Add avg_order_value to fct_daily_revenue.

    Expected:
    - fct_daily_revenue: WARNING (incremental + on_schema_change=fail, schema changed)
    - Operations should include BUILD FAILURE
    """

    def test_add_avg_order_value(self, tmp_path: Path):
        modified_fct_daily_revenue = """\
WITH enriched AS (
    SELECT * FROM int_order_enriched
)
SELECT
    order_date,
    SUM(total_amount) AS total_sales,
    COUNT(DISTINCT order_id) AS order_count,
    COUNT(DISTINCT customer_id) AS unique_customers,
    AVG(total_amount) AS avg_order_value
FROM enriched
GROUP BY order_date
"""
        base_sqls = dict(ALL_SQLS)
        current_sqls = dict(ALL_SQLS)
        current_sqls["fct_daily_revenue"] = modified_fct_daily_revenue

        manifest = _build_manifest()
        project_dir = _setup_project(
            tmp_path,
            base_sqls=base_sqls,
            current_sqls=current_sqls,
            manifest=manifest,
        )

        exit_code, result = _run_check(project_dir)

        # Only fct_daily_revenue should appear (only model changed)
        assert len(result["models"]) == 1

        fdr = _find_model(result, "fct_daily_revenue")
        assert fdr is not None, "fct_daily_revenue should appear in results"

        # Safety = WARNING (incremental + fail + schema changed)
        assert fdr["safety"] == "warning"

        # Operations should include BUILD FAILURE
        ops = [op["operation"] for op in fdr["operations"]]
        assert any("BUILD FAILURE" in op for op in ops)

        # Column diff: avg_order_value added, nothing removed
        assert "avg_order_value" in fdr["columns_added"]
        assert len(fdr["columns_removed"]) == 0

        # Exit code should be 2 (warning)
        assert exit_code == 2


# ---------------------------------------------------------------------------
# Scenario C — Refactor staging model column rename
# ---------------------------------------------------------------------------


class TestScenarioC_RefactorStagingColumnRename:
    """Rename total_amount -> order_total in stg_orders.

    int_order_enriched still references total_amount, so cascade should catch it.

    Expected:
    - stg_orders: SAFE (view, CREATE OR REPLACE)
    - cascade: int_order_enriched references dropped total_amount
    """

    def test_rename_column_cascade(self, tmp_path: Path):
        modified_stg_orders = """\
SELECT
    order_id,
    customer_id,
    order_date,
    status,
    total_amount AS order_total
FROM raw.orders
"""
        base_sqls = dict(ALL_SQLS)
        current_sqls = dict(ALL_SQLS)
        current_sqls["stg_orders"] = modified_stg_orders

        manifest = _build_manifest()
        project_dir = _setup_project(
            tmp_path,
            base_sqls=base_sqls,
            current_sqls=current_sqls,
            manifest=manifest,
        )

        exit_code, result = _run_check(project_dir)

        stg = _find_model(result, "stg_orders")
        assert stg is not None, "stg_orders should appear in results"

        # stg_orders is a view — CREATE OR REPLACE VIEW is always safe at DDL level
        ops = [op["operation"] for op in stg["operations"]]
        assert any("CREATE OR REPLACE VIEW" in op for op in ops)

        # Note: predict_ddl for view materialization does NOT populate
        # columns_added/columns_removed (views are always CREATE OR REPLACE,
        # so column diff is irrelevant for DDL safety). The cascade analysis
        # computes the diff internally from raw column extraction results.

        # Cascade: int_order_enriched references total_amount (dropped column)
        # The cascade analysis correctly detects that total_amount was removed
        # from stg_orders output and flags downstream models that reference it.
        assert "downstream_impacts" in stg
        impacts = stg["downstream_impacts"]

        broken_refs = [i for i in impacts if i["risk"] == "broken_ref"]
        broken_model_names = [i["model_name"] for i in broken_refs]
        assert "int_order_enriched" in broken_model_names, (
            "int_order_enriched should be flagged for broken_ref on total_amount"
        )

        # The cascade regex also catches total_amount references in deeper
        # downstream models (int_customer_metrics, fct_daily_revenue, etc.)
        # because their compiled SQL contains the literal text "total_amount"
        assert len(broken_refs) >= 1, "At least int_order_enriched should have broken_ref"

        # With a broken_ref cascade, safety should be escalated to DESTRUCTIVE
        assert stg["safety"] == "destructive"

        # Exit code should be 1 (destructive due to cascade)
        assert exit_code == 1


# ---------------------------------------------------------------------------
# Scenario D — Safe change: add column to table model
# ---------------------------------------------------------------------------


class TestScenarioD_SafeAddColumnToTable:
    """Add is_active to dim_products (table materialization).

    Expected:
    - dim_products: SAFE (table, CREATE OR REPLACE TABLE)
    - Exit code 0
    """

    def test_add_is_active_safe(self, tmp_path: Path):
        modified_dim_products = """\
SELECT
    product_id,
    name,
    price,
    category,
    TRUE AS is_active
FROM stg_products
"""
        base_sqls = dict(ALL_SQLS)
        current_sqls = dict(ALL_SQLS)
        current_sqls["dim_products"] = modified_dim_products

        manifest = _build_manifest()
        project_dir = _setup_project(
            tmp_path,
            base_sqls=base_sqls,
            current_sqls=current_sqls,
            manifest=manifest,
        )

        exit_code, result = _run_check(project_dir)

        # Only dim_products should appear (only model changed)
        assert len(result["models"]) == 1

        dim = _find_model(result, "dim_products")
        assert dim is not None, "dim_products should appear in results"

        # Safety = SAFE (table materialization, CREATE OR REPLACE TABLE)
        assert dim["safety"] == "safe"

        # Operations should include CREATE OR REPLACE TABLE
        ops = [op["operation"] for op in dim["operations"]]
        assert any("CREATE OR REPLACE TABLE" in op for op in ops)

        # Exit code = 0 (all safe)
        assert exit_code == 0


# ---------------------------------------------------------------------------
# Bonus: Verify baseline has 15 models and no changes yields empty result
# ---------------------------------------------------------------------------


class TestBaselineNoChanges:
    """When no models change, dbt-plan should report no changes."""

    def test_no_changes_exit_zero(self, tmp_path: Path):
        manifest = _build_manifest()
        project_dir = _setup_project(
            tmp_path,
            base_sqls=dict(ALL_SQLS),
            current_sqls=dict(ALL_SQLS),
            manifest=manifest,
        )

        exit_code, result = _run_check(project_dir)

        assert result["models"] == []
        assert result["summary"]["total"] == 0
        assert exit_code == 0

    def test_project_has_15_models(self):
        """Verify we actually have 15 models in the project."""
        assert len(ALL_SQLS) == 15
        assert len(MODEL_CONFIGS) == 15

        manifest = _build_manifest()
        model_nodes = [nid for nid in manifest["nodes"] if nid.startswith("model.")]
        assert len(model_nodes) == 15
