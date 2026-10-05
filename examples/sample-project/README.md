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

Canonical findings (before policy)

- SAFE ddl.replace_table: model.sample.dim_customers -> model.sample.dim_customers; CREATE OR REPLACE TABLE
  Evidence: compiled_sql / exact / column_diff_checked; raw risk: safe; waiver eligible: true
  Columns: country, created_at, customer_id, customer_tier; added: (none); removed: (none)
  Compiled SQL: target/compiled/sample/models/dim_customers.sql

- SAFE ddl.replace_table: model.sample.dim_publishers -> model.sample.dim_publishers; CREATE OR REPLACE TABLE
  Evidence: compiled_sql / exact / column_diff_checked; raw risk: safe; waiver eligible: true
  Columns: end_date, publisher_id, publisher_name, start_date; added: (none); removed: (none)
  Compiled SQL: target/compiled/sample/models/dim_publishers.sql

- SAFE ddl.add_column: model.sample.fct_daily_sales -> model.sample.fct_daily_sales; ADD COLUMN; column: total_sales
  Evidence: compiled_sql / exact / column_diff_checked; raw risk: safe; waiver eligible: true
  Columns: order_count, order_date, shipping_info, store_id, total_sales, unique_customers; added: total_sales; removed: (none)
  Compiled SQL: target/compiled/sample/models/fct_daily_sales.sql

- DESTRUCTIVE cascade.broken_ref: model.sample.int_order_enriched -> model.sample.fct_daily_sales; reads dropped column(s): shipping_info
  Evidence: legacy_cascade / unknown / provenance_unavailable; raw risk: broken_ref; waiver eligible: false
  Review: provenance_unavailable

- DESTRUCTIVE ddl.drop_column: model.sample.int_order_enriched -> model.sample.int_order_enriched; DROP COLUMN; column: billing_info
  Evidence: compiled_sql / exact / column_diff_checked; raw risk: destructive; waiver eligible: true
  Columns: billing_info, billing_method, customer_id, order_date, order_id, revenue, shipping_city, shipping_info, store_id; added: billing_method, shipping_city; removed: billing_info, shipping_info
  Compiled SQL: target/compiled/sample/models/int_order_enriched.sql

- DESTRUCTIVE ddl.add_column: model.sample.int_order_enriched -> model.sample.int_order_enriched; ADD COLUMN; column: billing_method
  Evidence: compiled_sql / exact / column_diff_checked; raw risk: destructive; waiver eligible: true
  Columns: billing_info, billing_method, customer_id, order_date, order_id, revenue, shipping_city, shipping_info, store_id; added: billing_method, shipping_city; removed: billing_info, shipping_info
  Compiled SQL: target/compiled/sample/models/int_order_enriched.sql

- DESTRUCTIVE ddl.add_column: model.sample.int_order_enriched -> model.sample.int_order_enriched; ADD COLUMN; column: shipping_city
  Evidence: compiled_sql / exact / column_diff_checked; raw risk: destructive; waiver eligible: true
  Columns: billing_info, billing_method, customer_id, order_date, order_id, revenue, shipping_city, shipping_info, store_id; added: billing_method, shipping_city; removed: billing_info, shipping_info
  Compiled SQL: target/compiled/sample/models/int_order_enriched.sql

- DESTRUCTIVE ddl.drop_column: model.sample.int_order_enriched -> model.sample.int_order_enriched; DROP COLUMN; column: shipping_info
  Evidence: compiled_sql / exact / column_diff_checked; raw risk: destructive; waiver eligible: true
  Columns: billing_info, billing_method, customer_id, order_date, order_id, revenue, shipping_city, shipping_info, store_id; added: billing_method, shipping_city; removed: billing_info, shipping_info
  Compiled SQL: target/compiled/sample/models/int_order_enriched.sql

dbt-plan: 4 checked, 3 safe, 0 warning, 1 destructive, 1 cascade risk(s)
```

The check exits **1** for the destructive change. The demonstration script prints
all three formats and reports that exit code; the script itself completes with 0.
The committed [output.txt](output.txt) and the use-cases page are checked against
a fresh CLI invocation by `tests/test_audit24_docs.py`.

`base/` is the saved baseline; `current/target/` holds the current compiled SQL
and manifest. These files illustrate analysis, not a runnable dbt project.
