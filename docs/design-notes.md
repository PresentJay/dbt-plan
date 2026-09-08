# Design notes

Why dbt-plan is shaped the way it is. Read this before proposing a change that
crosses one of these lines — most rejected ideas are rejected for a reason
recorded here rather than on their merits.

## The tool never connects to a warehouse

dbt-plan reads compiled SQL, `manifest.json`, and model config. That is the whole
input. The analysis does not query the warehouse or simulate a run. The optional
`dbt-plan run` CLI orchestrates Git and invokes the user-configured dbt compile
command; that preparation can require credentials and execute macros.

This is a deliberate constraint, not a missing feature:

- On the Fusion engine, which compiles without a warehouse connection, it runs on
  a pull request from a fork where no credentials exist and none should. On dbt
  Core it does not: dbt-plan never connects, but `dbt compile` does, so producing
  the input needs the secrets a fork pull request is denied. The boundary under
  Fusion is introspective macros — `run_query`, `get_columns_in_relation` — whose
  models fail to compile and are then reported as an incomplete compile rather
  than silently skipped.
- It cannot fail because a warehouse is down, slow, or rate-limiting, so it can
  sit in the required-checks list without becoming a flaky gate.
- There is no query cost, and no way for a static analysis pass to accidentally
  touch production.
- Contributors can reproduce any bug from a `.sql` file, which is why the bug
  report form asks for one.

The cost is real: without the warehouse's current schema, some questions cannot
be answered. `SELECT *` cannot always be expanded, and an inferred warehouse type
cannot be verified. Explicit cast changes remain visible in compiled SQL. The tool reports a warning in those cases rather than
guessing. That trade is the point — see the next section.

## A false warning is acceptable; a false safe is not

The tool exists to be believed when it stays quiet. Someone who sees `SAFE` and
merges a column drop has been actively harmed by the tool; someone who sees a
warning on a harmless change is mildly annoyed and moves on.

Everything follows from that asymmetry:

- `extract_columns` returns `None` on a parse failure. It never returns a partial
  or best-guess column list.
- Ambiguous columns — duplicates from a join, unaliased expressions — produce
  `REVIEW REQUIRED` rather than a verdict.
- A removed model is `DESTRUCTIVE` regardless of materialization, because the
  tool cannot know whether the object was dropped by hand afterwards.
- An unrecognised `on_schema_change` value is a warning, not a default to the
  safest interpretation.

Users who find a specific warning noisy can silence it with `ignore_models`, or
accept a specific destructive change with `--acknowledge`. Both are opt-in and
name the model explicitly. There is deliberately no blanket "acknowledge
everything": it would let changes that arrived after the review ride along behind
it, which is exactly the failure this asymmetry is built to prevent.

## Column extraction uses SQLGlot

Compiled dbt SQL is real warehouse SQL — window functions, `QUALIFY`, VARIANT
path access, `SELECT * EXCEPT(...)`, CTE chains. A regex approach fails on the
patterns that matter most, and failing silently on those would produce exactly
the false safes described above.

SQLGlot parses many of these patterns and offers multiple SQL dialects through
`--dialect`; it does not cover every dbt adapter or every grammar extension.
It has no dependencies of its own — which keeps dbt-plan
installable into a CI job without dragging in a tree.

Its version is a correctness boundary, not a packaging detail. Before 28.0.0,
`SELECT * EXCEPT(revenue)` extracted as plain `*`; a model that dropped a column
that way produced identical column lists on both sides and was reported safe. The
declared floor is `>=28.0.0` and the `minimum-deps` CI job runs the suite pinned
to it so the floor cannot quietly rot.

## Risk comes from materialization × on_schema_change

dbt decides what DDL to emit from the model's materialization and, for
incremental models, its `on_schema_change` setting. The prediction table in the
README mirrors that logic:

- `table` and `view` rebuild the object wholesale, so a column change is safe.
- `ephemeral` has no physical object at all.
- `incremental` is where the risk lives. `ignore` emits no DDL; `append_new_columns`
  only adds; `sync_all_columns` adds *and drops*, which is the one combination
  that can destroy data on a column removal.
- `snapshot` is left as `REVIEW REQUIRED`. Snapshot schema evolution has enough
  edge cases that a confident verdict would be dishonest.

`full_refresh` is out of scope: it is a runtime flag decided by whoever invokes
dbt, not something visible in the files.

## Cascade analysis resolves reads, with a conservative fallback

The manifest's `child_map` supplies the downstream graph. SQLGlot then qualifies
column reads against a schema derived from the project's compiled SQL, using the
manifest's relation names and aliases. This distinguishes a read from the changed
relation from a same-named column on another relation, a comment, or a literal.
The report says `reads dropped column(s)` when resolution succeeds.

If parsing or qualification cannot establish the read, the resolver returns
`None` and the caller uses a wider word-boundary text search. That report says
`references dropped column(s)`. An unresolved alias or star passthrough must not
be treated as proof of no dependency. Fix missing semantic evidence at the
resolver boundary instead of adding ever-broader regex patterns.

The [sample project](../examples/sample-project/README.md) exercises the resolved
path, with its output checked by an executable regression test.

## Snapshot-and-compare, rather than reading git

dbt-plan compares two directories of compiled SQL. `dbt-plan snapshot` saves the
baseline; `dbt-plan check` diffs against it.

Working from compiled output rather than git history means Jinja, macros, package
models, and generated SQL are all already resolved — the tool compares what dbt
will actually run, not what the templates look like. `dbt-plan run` wraps the
whole sequence for local use, stashing uncommitted work to compile the baseline
and restoring it afterwards.

That stash is the most dangerous thing the tool touches, so its lifetime lives in
`stash.py` behind a context manager: there is no path through the block that
skips the restore, including the `sys.exit()` calls that several helpers in
`cli.py` make.

## Out of scope

| Not planned | Why |
|-------------|-----|
| `dbt run` simulation | Runtime behaviour; the tool is static analysis |
| INFORMATION_SCHEMA queries | Needs a warehouse connection |
| Type changes on columns with no explicit `CAST` | The type is whatever the warehouse assigned, so seeing a change would mean asking it. Columns cast explicitly on *both* sides are compared since 0.8.0 — that comparison is compiled SQL against compiled SQL, which needs no connection |
| `seed` / `source` change detection | Neither produces compiled SQL to diff |
| `pre_hook` / `post_hook` DDL analysis | Arbitrary SQL in arbitrary places; high complexity, low signal |
| `full_refresh` judgment | A runtime flag, decided outside the files |

## Decisions retained from the gap audit

The five audit perspectives produced 31 recommendations against proposed work.
The table preserves their order and reasons from the audit behind
[#171](https://github.com/PresentJay/dbt-plan/issues/171), including repeated
recommendations. These are design decisions and sequencing constraints, not a
claim that every associated implementation defect is already fixed. A condition
such as “before refusal handling is sound” is not a permanent feature ban.

| Audit item | Decision and reason |
|---|---|
| 1 | Do not apply incremental rules to a non-incremental materialization merely because it inherits `on_schema_change`; folder config is not evidence that its adapter reads that option. |
| 2 | Do not guess source/seed star columns or simulate snapshot strategies. Manifest declarations may support a marked fallback; snapshots need an explicit review result. |
| 3 | Defer additional output surfaces and policy knobs until false-safe paths and refusal exit semantics are fixed; every integration repeats the verdict. |
| 4 | Fix unresolved cascade reads at the resolver boundary instead of widening regex fallback; lack of evidence must not become an empty read set. |
| 5 | Do not model full refresh; it depends on runtime choices and intentional changes can be acknowledged individually. |
| 6 | Do not expose stash/checkout/compile as an MCP `run` tool in the shared editing tree; restoration cannot protect another agent's concurrent edits. See the MCP decision below. |
| 7 | Do not let warning policy turn a refusal into success; “could not inspect” and a completed finding need distinct treatment. |
| 8 | Do not reproduce dbt's complete selector grammar in static `check`; reject unsupported forms. A future compilation workflow can delegate selection to dbt. |
| 9 | Do not query warehouse schema or read `catalog.json` to resolve stars; retain the files-only input boundary and an explicit review result. |
| 10 | Defer new notification/output integrations until the local run loop and reproduced false-safe cases are reliable; reach cannot repair trust. |
| 11 | Do not add full-refresh or dbt-run simulation; compiled SQL and known DDL rules are the available evidence. |
| 12 | Do not add a warehouse fallback for sources, seeds, or aliases; alias resolution can use manifest relation names without credentials. |
| 13 | Do not broaden waivers while refusals can be suppressed; wider matching creates more silent pass paths. |
| 14 | Defer new CI generators and output formats until exit codes have one contract; otherwise each copies the ambiguity. |
| 15 | Do not run `dbt clean` or erase `target/` to detect removals; these are user artifacts, and manifest membership can establish removal for plain `check` too. |
| 16 | Keep CLI compile commands free of implicit shell evaluation; document Action shell semantics or align the Action to argv, rather than weaken the CLI. |
| 17 | Do not infer runtime full-refresh, materialized-view policy, or Python DataFrame behavior; the files cannot justify those predictions. |
| 18 | Do not add an optional warehouse-connected verify/data-diff mode; it changes the credential and dependency boundary of the tool. |
| 19 | Do not classify WHERE/JOIN/aggregation edits as semantically safe data changes; SQL shape alone cannot establish data correctness. |
| 20 | Do not build a full selector engine; name/graph selection plus loud refusal of unsupported operators is the intended static scope. |
| 21 | Defer wider verdict broadcasting until false-safe cases have real-dbt coverage; remove promises of automatic comments/labels/Slack that the generator does not implement. |
| 22 | Do not simulate materialized-view configuration or dynamic-table refresh; adapter-specific runtime behavior needs review, not guessed incremental rules. |
| 23 | Do not add a YAML dependency for the small config surface; document the accepted flat subset and reject unsupported structures. |
| 24 | Do not infer columns from Python DataFrame programs; the problem is unbounded for this analyzer, so report unsupported analysis. |
| 25 | Do not add acknowledge-all or wider ignore/exit overrides while they can hide refusals; preserve specific, visible review decisions. |
| 26 | Do not add even optional warehouse verification; fix file-based false-safe paths instead of requiring credentials to mask them. |
| 27 | Do not implement a Jinja/var renderer or materialized-view simulator; dbt owns compilation and runtime configuration semantics. |
| 28 | Do not add waiver globs before ignored models remain visible in the report; a folder pattern must not become a silent off-switch. |
| 29 | Do not extend pass-through policy until completed findings and refusals are distinguished; a policy must not turn lack of analysis into approval. |
| 30 | Defer new node kinds and output surfaces until every shipped entry point detects stale or partial inputs; more coverage on an untrusted baseline is misleading. |
| 31 | Do not patch resolver misses with smarter text search; repair the semantic error that incorrectly returned an empty read set. |

### MCP compilation remains a CLI workflow

[#148](https://github.com/PresentJay/dbt-plan/issues/148) requested an MCP `run`
tool after target-directory support shipped. The existing CLI's restoration tests
are necessary, but do not establish exclusive ownership of a working tree while
an agent edits it. Delegating directly to that CLI would expose stash, checkout,
and arbitrary configured compilation through an interface currently used for
analysis. The proposed tool is therefore declined in this scope.

MCP `plan` reads prepared artifacts; MCP `snapshot` writes a saved baseline but
does not compile or change Git revisions. Use the existing CLI `dbt-plan run`
explicitly when compilation and revision changes are desired, with exclusive use
of the working tree. A future proposal would need an isolated baseline workspace,
a clear execution contract, and concurrency/restoration evidence; adding a thin
subprocess wrapper alone does not solve the shared-tree problem.

See [analysis limits](analysis-limits.md) for adapter fallback, cross-project
consumers, and unresolved source/seed stars, and [CI integration](ci-integration.md#컴파일-명령과-dbt-패키지)
for the distinction between CLI argv and Action shell commands.
