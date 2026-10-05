### dbt-plan -- 1 model(s) changed
  dialect: duckdb (manifest; adapter: duckdb)
  baseline: file-only-example, 2000-01-01T00:00:00+00:00


🔴 **DESTRUCTIVE** `root` (view)
- CREATE OR REPLACE VIEW
- Downstream: mid, reader, report (3 model(s))
- 🔴 **INHERITED_DROP** `reader`: file unchanged, loses amount from upstream -- DROP COLUMN amount
- 🔴 **BROKEN_REF** `report`: reads dropped column(s): amount

#### Causal explanations (before policy)

- DESTRUCTIVE cascade.inherited\_drop: source model.shop.root ([models/root.sql](./models/root.sql)); affected model.shop.reader ([models/reader.sql](./models/reader.sql))
  file unchanged, loses amount from upstream -- DROP COLUMN amount
  Source change: added (not recorded); removed amount
  Affected/evidence columns: (not recorded); evidence: legacy\_cascade / unknown / provenance\_unavailable; raw risk: inherited\_drop; waiver eligible: false
  Review: provenance\_unavailable
  Source config: materialized=view; on_schema_change=None
  Affected config: materialized=incremental; on_schema_change=sync\_all\_columns
  Step: model.shop.root ([models/root.sql](./models/root.sql)) -> model.shop.mid ([models/mid.sql](./models/mid.sql)); candidate dependency; column flow unknown; removed (not recorded)
    Config: materialized=ephemeral; on_schema_change=None
  Step: model.shop.mid ([models/mid.sql](./models/mid.sql)) -> model.shop.reader ([models/reader.sql](./models/reader.sql)); candidate dependency; column flow unknown; removed (not recorded)
    Config: materialized=incremental; on_schema_change=sync\_all\_columns

- DESTRUCTIVE cascade.broken\_ref: source model.shop.root ([models/root.sql](./models/root.sql)); affected model.shop.report ([models/report.sql](./models/report.sql))
  reads dropped column(s): amount
  Source change: added (not recorded); removed amount
  Affected/evidence columns: (not recorded); evidence: legacy\_cascade / unknown / provenance\_unavailable; raw risk: broken\_ref; waiver eligible: false
  Review: provenance\_unavailable
  Source config: materialized=view; on_schema_change=None
  Affected config: materialized=view; on_schema_change=None
  Step: model.shop.root ([models/root.sql](./models/root.sql)) -> model.shop.mid ([models/mid.sql](./models/mid.sql)); candidate dependency; column flow unknown; removed (not recorded)
    Config: materialized=ephemeral; on_schema_change=None
  Step: model.shop.mid ([models/mid.sql](./models/mid.sql)) -> model.shop.reader ([models/reader.sql](./models/reader.sql)); candidate dependency; column flow unknown; removed (not recorded)
    Config: materialized=incremental; on_schema_change=sync\_all\_columns
  Step: model.shop.reader ([models/reader.sql](./models/reader.sql)) -> model.shop.report ([models/report.sql](./models/report.sql)); exact direct compiled read; root attribution not established; removed amount
    Config: materialized=view; on_schema_change=None

- SAFE ddl.replace\_view: source model.shop.root ([models/root.sql](./models/root.sql)); affected model.shop.root ([models/root.sql](./models/root.sql))
  CREATE OR REPLACE VIEW
  Source change: added (not recorded); removed amount
  Affected/evidence columns: amount, id; evidence: compiled\_sql / exact / column\_diff\_checked; raw risk: safe; waiver eligible: true
  Compiled SQL (not a source/Jinja line): target/compiled/shop/models/root.sql
  Source config: materialized=view; on_schema_change=None
  Affected config: materialized=view; on_schema_change=None

  Observation: model.shop.mid ([models/mid.sql](./models/mid.sql)) -> model.shop.reader ([models/reader.sql](./models/reader.sql)); resolved read check; not a column-flow edge; columns (not recorded); read\_checked
Explanation detail omitted: 0 associations; 1 graph edges not displayed. Full relevant graph and findings: JSON.
External consumer coverage: unknown. Exposures are declared consumers only.


`dbt-plan: 1 checked, 0 safe, 0 warning, 1 destructive, 2 cascade risk(s)`
