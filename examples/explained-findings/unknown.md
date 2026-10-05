### dbt-plan -- 1 model(s) changed
  dialect: duckdb (manifest; adapter: duckdb)
  baseline: file-only-example, 2000-01-01T00:00:00+00:00


✅ **SAFE** `root` (view)
- CREATE OR REPLACE VIEW
- Downstream: reader (1 model(s))

#### Causal explanations (before policy)

- WARNING input.refusal: source model.shop.reader ([models/reader.sql](./models/reader.sql)); affected model.shop.reader ([models/reader.sql](./models/reader.sql))
  Unresolved compiled SQL read from root while checking root; affected columns: amount
  Source change: not recorded
  Affected/evidence columns: (not recorded); evidence: input / unknown / read\_unresolved; raw risk: warning; waiver eligible: false
  Review: read\_unresolved
  Source config: materialized=view; on_schema_change=None
  Affected config: materialized=view; on_schema_change=None

- SAFE ddl.replace\_view: source model.shop.root ([models/root.sql](./models/root.sql)); affected model.shop.root ([models/root.sql](./models/root.sql))
  CREATE OR REPLACE VIEW
  Source change: added (not recorded); removed amount
  Affected/evidence columns: amount, id; evidence: compiled\_sql / exact / column\_diff\_checked; raw risk: safe; waiver eligible: true
  Compiled SQL (not a source/Jinja line): target/compiled/shop/models/root.sql
  Source config: materialized=view; on_schema_change=None
  Affected config: materialized=view; on_schema_change=None

  Observation: model.shop.root ([models/root.sql](./models/root.sql)) -> model.shop.reader ([models/reader.sql](./models/reader.sql)); unknown read check; not a column-flow edge; columns (not recorded); read\_unresolved
Explanation detail omitted: 0 associations; 1 graph edges not displayed. Full relevant graph and findings: JSON.
External consumer coverage: unknown. Exposures are declared consumers only.


`dbt-plan: 1 checked, 1 safe, 0 warning, 0 destructive`
