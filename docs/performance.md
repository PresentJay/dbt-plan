# Measuring CLI performance

Run from a checkout with the test environment installed:

```bash
uv sync --extra test
uv run --no-sync python scripts/benchmark_cli.py \
  --models 50 200 1000 --repeat 5 --warmup 1 --output benchmark.json
```

The script invokes this checkout's `python -m dbt_plan.cli check --format json`
in a fresh subprocess for each sample. Wall time includes interpreter startup,
imports, artifact reads, analysis, and JSON serialization. Generating fixtures,
creating the baseline snapshot, and validating the returned JSON are outside the
timed interval. **dbt compilation is not measured**; these inputs are synthetic
compiled SQL and manifest files. Measure `dbt compile` separately on your real
project, with its own adapter/profile/macros and cache state.

Each model is incremental with `on_schema_change: sync_all_columns`, starting
with `id` and `amount`. Groups of ten form a root with up to nine direct consumers,
so dropping a root column also exercises cascade analysis. The scenarios are:

- `unchanged`: identical current and baseline artifacts.
- `one_changed`: the first root drops `amount`; its consumers remain unchanged.
- `all_changed`: every model drops `amount`.

The defaults run 50, 200, and 1,000 models, one unrecorded warm-up per scenario,
then five recorded subprocesses. The benchmark requires the expected changed
model count, no parse failures, exact dropped columns, and the expected verdict
and exit code on **every** run, including warm-ups. An empty or incorrect analysis
fails instead of producing a deceptively fast sample.

JSON records every sample, minimum/median/maximum, Python/SQLGlot versions,
platform, machine architecture, logical CPUs, checkout revision and dirty state.
`source_sha256` identifies the analysis Python files and benchmark script even
when the checkout contains uncommitted changes. The version string alone cannot
identify unreleased code. Filesystem caches are warm; other machine load is not
controlled. These small SQL projections and shallow fan-out do not model every
real query, macro, contract, or deep dependency graph.

## Recorded run

See [all samples and environment](benchmarks/cli-2026-09-09.json). This is a local
measurement of the unreleased checkout, not a claim about a published package or
all projects. The previous undocumented README and use-case timings have been
removed. The 200-model target in CLAUDE.md remains a design goal, not a guaranteed
latency bound.

Environment: CPython 3.12.9, SQLGlot 30.17.0,
macOS-26.5.1-arm64-arm-64bit, 14 logical CPUs. Five samples after one warm-up;
values below are median seconds. See the JSON for ranges and individual samples.

| Models | Unchanged | One changed | All changed |
|---:|---:|---:|---:|
| 50 | 0.108 | 0.166 | 0.178 |
| 200 | 0.206 | 0.243 | 0.578 |
| 1000 | 0.511 | 1.313 | 4.491 |

CI smoke-tests the benchmark at 12 models and validates its results. It does not
assert these wall times on shared runners. Rerun the full command for performance
changes and compare identical workloads, toolchains and machines.
