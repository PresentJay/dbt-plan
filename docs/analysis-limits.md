# What the report can see

A report describes the compiled SQL and manifest supplied to it. A passing exit
code is a policy result, not proof that a warehouse build or downstream system
will succeed.

## Adapters and SQL dialects

An explicit `--dialect` takes precedence over configuration; otherwise dbt-plan
maps the manifest's `metadata.adapter_type` to a SQLGlot dialect. An adapter with
no mapping falls back to Snowflake. `check -v` prints the resolved dialect.
For example, Vertica, Firebolt, Greenplum, SingleStore and Impala currently have
no adapter mapping. Set a suitable supported `--dialect` explicitly if one exists;
SQLGlot does not necessarily implement the adapter's full SQL grammar. Parsing
success alone does not validate warehouse semantics. A parse failure requires
review, and changing dialect to suppress a warning is not evidence of safety.

## dbt Mesh and external consumers

Cascade analysis walks dependencies in the supplied manifests. A separate dbt
project's `ref('producer_project', 'public_model')` is recorded in that consumer's
manifest, which a check of the producer is not given. `access: public` and `group`
do not supply the missing dependency graph. A report with no downstream impacts
therefore says nothing about consumers in other projects, dashboards not declared
as exposures, or external SQL jobs. Review those consumers separately when a
public model changes.

## Stars over derived tables

A bare `SELECT *` over a single derived table can resolve when its inner
`SELECT` names every output explicitly, as in
`SELECT * FROM (SELECT CAST(1 AS BIGINT) AS book_id) AS empty_table WHERE 1 = 0`.
Empty-table macros use this pattern to declare an incremental model's schema.
Column names and explicit casts come from the SQL rather than manifest fallback.

This bounded expansion does not resolve qualified stars over derived tables,
joins, source-column alias lists, inner CTE scopes, set operations or nested
stars. Value-table SELECTs, column-expanding expressions such as DuckDB
`COLUMNS`, pivots and unsupported star modifiers also require review.

## Stars over sources and seeds

The relation resolver indexes compiled model relations. Sources and seeds do not
provide compiled model SQL, so `SELECT *` through either may remain unresolved,
even when a seed node has documented columns in the manifest. Documented columns
on the consuming model can supply a fallback, but are declarations rather than
proof of its actual output. The report must retain that uncertainty, including
for enforced contracts. Prefer explicit projection lists when practical.

Seed/source change detection is outside the compiled-SQL diff. This is distinct
from star resolution: supporting manifest-based resolution in future would not
make seed data or external source schema changes visible. dbt-plan does not query
`information_schema`, load `catalog.json`, or contact a warehouse to fill gaps.

## Comment-only dependencies

A `ref()` inside a SQL comment can create a manifest dependency even though the
compiled query never reads that relation. Cascade traversal excludes such an edge
only when current compiled SQL proves the physical relation occurs in a comment
and is absent from a single readable query. Graph selection still uses the
original manifest, including these edges.

Explicit `depends_on` comments, user-defined or dynamic functions, unknown
relations, Python/ephemeral/custom models, hooks and unverified compilation retain
the dependency. Alternate real dependency paths still propagate findings. This
bounded check can leave false warnings when it cannot prove an edge is inactive.

## Compilation and runtime

`check` and MCP `plan` analyze existing files. `run` invokes the user's compile
command for both revisions; that command can need credentials and execute dbt
macros. Freshness checks help reject stale or incomplete inputs, but are not a
substitute for a successful complete compile. When a source checkout is present,
model and macro content can be compared with manifest evidence; an artifact-only
checkout cannot validate source edits or deletions it was not given. Matching
compilation invocation results provide stronger evidence than timestamps, which
remain a fallback when that evidence is absent. Chain manual commands with
`dbt compile && dbt-plan check`.

SQL snapshot provenance accepts legacy full-file checksums. For dbt 1.12's
whitespace-normalized checksums it additionally matches every named snapshot
block body byte for byte, with only whitespace or Jinja comments outside standard
snapshot tags. This prevents changes to spaces in SQL literals or line-comment
newlines from passing on the normalized hash alone. Missing block evidence or
unsupported outer templating still requires review. Snapshot definition changes
remain warnings; this does not predict snapshot DDL.

The tool does not simulate `dbt run`, `--full-refresh`, hooks, materialized-view
refresh policy, Python DataFrame operations, or data-value changes. Explicit cast
changes can be inspected from SQL; inferred warehouse types cannot be proven
without the warehouse. See [design notes](design-notes.md) for these boundaries
and [CI integration](ci-integration.md#컴파일-명령과-dbt-패키지) for command execution.
