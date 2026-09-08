# September audit resolutions

This records the disposition of the 24 audit issues that remained open when this
follow-up began. It distinguishes implemented safeguards from unsupported analysis
and from a feature proposal deliberately declined. Reproduction tests are checked
in; no warehouse access was added to the analyzer.

| Issue | Resolution | Regression evidence |
|---|---|---|
| [#133](https://github.com/PresentJay/dbt-plan/issues/133) | Refuse incomplete compiled inputs using matching invocation results, node compilation metadata, and an age fallback. Selection retains its dependency and consumer checks. | `test_audit24_cli.py`: partial compile and selection cases; `test_audit24_review.py` |
| [#138](https://github.com/PresentJay/dbt-plan/issues/138) | Compare snapshot raw code/configuration in manifests, including projects without compiled SQL; changed snapshots require review. | `test_audit24_cli.py`: snapshot and snapshot-only cases |
| [#140](https://github.com/PresentJay/dbt-plan/issues/140) | Compare relation identity even when compiled SQL is unchanged; warn that database/schema/alias moves do not transfer incremental history. | `test_audit24_cli.py`: database/schema/alias parameterization |
| [#141](https://github.com/PresentJay/dbt-plan/issues/141) | A cast added or removed on one side requires review; the unknown type is printed rather than guessed. | `test_audit24_cli.py`, `test_cast_type_changes.py` |
| [#142](https://github.com/PresentJay/dbt-plan/issues/142) | Expand EXCLUDE/EXCEPT against known source columns; unresolved modifiers remain uncertainty. | `test_audit24_columns.py`: CTE/physical, qualified/unqualified, dialect cases |
| [#143](https://github.com/PresentJay/dbt-plan/issues/143) | Inspect compiled data-test SQL in addition to the generic test column; unreadable SQL requires review. | `test_audit24_cascade.py`: extra test predicates and unreadable SQL |
| [#144](https://github.com/PresentJay/dbt-plan/issues/144) | Python models are not missing SQL. Unchanged Python models are listed as unsupported analysis; changed ones explicitly require review. | `test_audit24_cli.py`: unchanged/changed Python |
| [#145](https://github.com/PresentJay/dbt-plan/issues/145) | **Already resolved concurrently by PR #199, integrated here:** unsupported graph operators and unknown names are execution errors, never empty successful checks. | `test_audit24_cli.py`, `test_select_graph.py` |
| [#146](https://github.com/PresentJay/dbt-plan/issues/146) | Document CLI argument splitting versus Action shell evaluation, wrapper scripts, and dependency installation for each revision. | `docs/ci-integration.md`; CLI and Action command construction reviewed |
| [#148](https://github.com/PresentJay/dbt-plan/issues/148) | **Declined:** no MCP `run` tool. Git checkout/stash/compile can conflict with concurrent agent edits. Use explicit CLI `run`; MCP `plan` remains artifact analysis and `snapshot` explicitly writes the baseline. | `docs/design-notes.md`: original audit recommendation 6 and MCP boundary |
| [#149](https://github.com/PresentJay/dbt-plan/issues/149) | `agent-setup --file` appends or creates the requested instruction file, preserving existing text and refusing duplicate sections. | `test_audit24_cli.py`, `test_agent_setup.py` |
| [#150](https://github.com/PresentJay/dbt-plan/issues/150) | Document unmapped dialects, dbt Mesh, and stars through seeds/sources, with workflow alternatives and explicit limits. | `docs/analysis-limits.md`, README, `CLAUDE.md` |
| [#151](https://github.com/PresentJay/dbt-plan/issues/151) | Add real DuckDB/dbt second-run tests for all four schema-change policies in both directions, plus ephemeral, materialization, and macro-only changes. | `test_dbt_e2e.py::TestAudit24RulesAgainstRealDbt`; required integration job rejects skips |
| [#152](https://github.com/PresentJay/dbt-plan/issues/152) | Preserve source provenance and baseline directory knowledge; detect missing/changed source files while allowing deletions after a proven successful compile. | `test_audit24_cli.py`: deleted macro negative/positive controls |
| [#154](https://github.com/PresentJay/dbt-plan/issues/154) | Retain known projection names alongside an uncertainty flag, so known drops remain visible. Unknown expressions still require review. | `test_audit24_columns.py`, `test_audit24_cli.py`: known drop with unnamed count |
| [#157](https://github.com/PresentJay/dbt-plan/issues/157) | Resolve unit-test fixture inputs to versioned and package-qualified model identities, including latest-version references. | `test_audit24_cascade.py` |
| [#159](https://github.com/PresentJay/dbt-plan/issues/159) | Compare contracts against independently extracted SQL columns, not their own manifest declaration; check unchanged downstream contracts too. | `test_audit24_cli.py`: unresolved and downstream stars |
| [#160](https://github.com/PresentJay/dbt-plan/issues/160) | Conservatively refuse quoted column identities on case-sensitive dialects rather than folding them into a false match. Quoted contract metadata is also uncertain. | `test_audit24_columns.py`, `test_audit24_cli.py` |
| [#163](https://github.com/PresentJay/dbt-plan/issues/163) | Repair the sample upstream projections and regenerate showcase output from the resolver-backed CLI result. | `test_audit24_docs.py`; `examples/sample-project/output.txt` |
| [#167](https://github.com/PresentJay/dbt-plan/issues/167) | Propagate inherited column loss through ephemeral models to downstream models and tests. | `test_audit24_cascade.py`, real ephemeral dbt case |
| [#168](https://github.com/PresentJay/dbt-plan/issues/168) | Reorder-only sync changes cause no schema DDL; scope model-specific refusals to selected models and their dependency/consumer graph, retaining shared-input warnings. | `test_column_ordering.py`, `test_audit24_cli.py` |
| [#169](https://github.com/PresentJay/dbt-plan/issues/169) | Attach downstream exposures to SAFE table/view replacements that remove columns, without inventing a destructive model operation. | `test_audit24_cascade.py` |
| [#170](https://github.com/PresentJay/dbt-plan/issues/170) | Record snapshot time/version/Git checkout revision and show dialect/baseline inputs in text, GitHub, and JSON reports. Legacy provenance stays explicitly unknown. | `test_audit24_cli.py`, formatter/JSON suites |
| [#171](https://github.com/PresentJay/dbt-plan/issues/171) | Preserve all 31 original excluded proposals with reasons and current dispositions. | `docs/design-notes.md`: numbered recommendations 1–31 |

## Verification boundaries

The new execution comparisons use dbt-core 1.11.7, dbt-duckdb 1.10.1 and a local,
disk-backed DuckDB database. They create the target before the second run; testing
only a first build would bypass the incremental path. They also assert that a
fresh successful compile does not produce incidental stale/uncompiled warnings.

Snowflake/Postgres quoted-identifier behavior is handled conservatively from SQL
syntax and documented identifier semantics, not from live warehouse measurements.
Full quote-aware column lineage is not claimed. Python DataFrame schemas, external
Mesh graphs, and warehouse introspection remain outside static analysis.

Source provenance checks require a source checkout. An artifact-only directory
cannot prove whether absent source code changed. A matching `run_results.json`
helps establish compile coverage; an ephemeral node is verified through its compiled
manifest SQL because dbt does not give it an execution result. Timestamp checks
remain a fallback, not a complete content provenance system.
