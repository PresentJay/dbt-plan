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

The tool does not simulate `dbt run`, `--full-refresh`, hooks, materialized-view
refresh policy, Python DataFrame operations, or data-value changes. Explicit cast
changes can be inspected from SQL; inferred warehouse types cannot be proven
without the warehouse. See [design notes](design-notes.md) for these boundaries
and [CI integration](ci-integration.md#컴파일-명령과-dbt-패키지) for command execution.
