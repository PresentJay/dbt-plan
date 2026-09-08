# Sample project

Run the static check against committed before/after SQL. No warehouse or dbt
installation is needed.

```bash
pip install dbt-plan
bash examples/sample-project/run-example.sh
```

`int_order_enriched` drops `shipping_info` and `billing_info` under
`sync_all_columns`. `fct_daily_sales` still reads `shipping_info`; the resolver
attributes that read to the changed relation. Its other inputs, `customer_id`
and `revenue`, exist in both versions of the upstream SQL.

`dim_customers` changes as a table, and `dim_publishers` is a new table.

## Expected text output

```text
dbt-plan -- 4 model(s) changed
  dialect: snowflake (default; adapter: unknown)
  baseline: unknown revision, unknown snapshot time


DESTRUCTIVE  int_order_enriched (incremental, sync_all_columns)
  ADD COLUMN  billing_method
  ADD COLUMN  shipping_city
  DROP COLUMN  billing_info
  DROP COLUMN  shipping_info
  Downstream: dim_customers, fct_daily_sales (2 model(s))
  >> BROKEN_REF  fct_daily_sales: reads dropped column(s): shipping_info

SAFE  dim_customers (table)
  CREATE OR REPLACE TABLE

SAFE  dim_publishers (table)
  CREATE OR REPLACE TABLE

SAFE  fct_daily_sales (incremental, append_new_columns)
  ADD COLUMN  total_sales

dbt-plan: 4 checked, 3 safe, 0 warning, 1 destructive, 1 cascade risk(s)
```

The check exits **1** for the destructive change. The demonstration script prints
all three formats and reports that exit code; the script itself completes with 0.
The committed [output.txt](output.txt) and the use-cases page are checked against
a fresh CLI invocation by `tests/test_audit24_docs.py`.

`base/` is the saved baseline; `current/target/` holds the current compiled SQL
and manifest. These files illustrate analysis, not a runnable dbt project.
