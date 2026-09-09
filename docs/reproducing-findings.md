# Reproducing a finding without a warehouse

dbt-plan reads compiled SQL and `manifest.json` from disk and never connects to a
database. That means every finding — and every CLI error — is reproducible with
nothing but a Python environment, a copy of the two revisions, and the commands
below. You do not need a warehouse account, a dbt profile, or any credentials.

This guide shows two levels of reproduction and what evidence to attach when you
file a report. If you are new here, read
[CONTRIBUTING](../CONTRIBUTING.md#development-setup) for setup first, and remember
the project rule: when analysis cannot decide, it reports uncertainty — a false
warning is fine, a false all-clear is not.

## Two levels of reproduction

### Parser-only: is the column list read wrong?

Use a one-line `extract_columns` check when the suspected bug is about how one
model's *projected names* are parsed: an alias is lost, a lookup leaks into the
schema, `SELECT * EXCEPT` expands to the wrong list. This is the smallest case
that can fail, and it exercises the exact code path a fixture regression test
pins down.

The invented bookstore domain is the right vocabulary for these examples — never
paste a private schema from a real warehouse into a report.

```python
from dbt_plan.columns import extract_columns

sql = "SELECT order_id, shipping_info FROM all_orders"
print(extract_columns(sql, dialect="snowflake"))
```

Output, independent of the shell working directory:

```text
['order_id', 'shipping_info']
```

State the dialect explicitly (the default is `snowflake`), and give the exact
expected list you believe is correct, separately from the list dbt-plan returned.
A mismatch between those two lists is the whole report for a parser bug.

### Full CLI report: is the verdict or downstream impact wrong?

Use the full report when the bug spans more than one model — a cascade `broken_ref`,
a materialization×`on_schema_change` verdict, a `sync_all_columns` removal, or a
configuration/selection issue. These need the baseline and current artifacts and a
`manifest.json`; a single `extract_columns` call cannot prove a cascade verdict
(see [What a minimized case must keep](#what-a-minimized-case-must-keep)).

The minimal full report runs against the committed sample project, which needs no
`dbt compile` at all:

```python
import json
import subprocess
import sys
from pathlib import Path

repo = Path("/absolute/path/to/dbt-plan").resolve()
base = repo / "examples" / "sample-project" / "base"
current = repo / "examples" / "sample-project" / "current"

proc = subprocess.run(
    [
        sys.executable,
        "-m",
        "dbt_plan.cli",
        "check",
        "--base-dir",
        str(base),
        "--project-dir",
        str(current),
        "--format",
        "json",
    ],
    capture_output=True,
    text=True,
    encoding="utf-8",
    check=False,
)

print("exit code:", proc.returncode)
print("stderr:", repr(proc.stderr))
report = json.loads(proc.stdout)   # a completed report, not an error body
print("summary:", report["summary"])
```

`check=False` is deliberate. The sample project contains a destructive change, so
this check exits 1 — throwing on it (`check=True`) would hide the completed report
under a `CalledProcessError`. You want the exit code *and* the report, so capture
them without `check=True`, never `shell=True`, and always pass the CLI as an
argument list. Run this from an editable install — `uv sync --extra test` or
`pip install -e ".[test]"` — so `sys.executable` points at an environment where
`dbt_plan` is importable.

Verified output, measured from an editable install at `805a3fd` on SQLGlot
30.17.0, identical whether run from the repository root or an unrelated
temporary directory (the fixture paths above are absolute):

```text
exit code: 1
stderr: ''
summary: {'total': 4, 'safe': 3, 'warning': 0, 'destructive': 1, 'cascade_risks': 1}
```

### Read the destructive model, not the whole report

The full JSON is bulky. Pull out the one model you care about instead of pasting
the report:

```python
model = next(m for m in report["models"] if m["model_name"] == "int_order_enriched")
print(model["safety"])
print(model["columns_removed"])
print(model["downstream_impacts"])
```

Verified output:

```text
destructive
['billing_info', 'shipping_info']
[{'model_name': 'fct_daily_sales', 'risk': 'broken_ref', 'reason': 'reads dropped column(s): shipping_info'}]
```

Here `int_order_enriched` drops two columns under `sync_all_columns`, so the check
exits 1 — a destructive finding, not a setup failure. Its downstream model
`fct_daily_sales` still reads `shipping_info`, which is the cascade risk.

### The execution-error control: exit code 3 is not an empty safe report

When an argument is invalid, argparse exits **3** (dbt-plan's execution-failure
code) with diagnostics on **stderr** and no completed report on stdout. Do not
parse the missing stdout as an empty safe report:

```python
proc = subprocess.run(
    [sys.executable, "-m", "dbt_plan.cli", "check", "--nope"],
    capture_output=True,
    text=True,
    encoding="utf-8",
    check=False,
)

print("exit code:", proc.returncode)
print("completed report on stdout:", bool(proc.stdout.strip()))
print("stderr:", proc.stderr.strip().splitlines()[-1])
```

Verified output:

```text
exit code: 3
completed report on stdout: False
stderr: dbt-plan: error: unrecognized arguments: --nope
```

The full contract is in [Exit codes and migration](exit-codes.md) — this guide
repeats only the part you need for a reproduction. The short version: 0 passed
policy, 1 destructive, 2 review required, 3 execution could not complete.

## Evidence to include with a report

A useful report lets the maintainer reproduce without guessing, and does not
expose private data.

**Include:**

- Version facts: dbt-plan version (`pip show dbt-plan` or `dbt-plan --version`),
  the Python version, and the SQLGlot version (`python -c "import sqlglot; print(sqlglot.__version__)"`).
  Findings can change between SQLGlot releases; the parser is a correctness boundary.
- The exact command or the `extract_columns` call you ran, and the dialect.
- Small baseline/current artifacts that make the finding appear. The bookstore
  domain from the fixtures is ideal: `orders`, `customers`, `book_prices`.
- Expected versus observed: the column list or verdict you think is right, and
  what dbt-plan returned.
- The process exit code and, for a CLI reproduction, whether a completed report
  was produced on stdout — that tells us a finding from an error apart.

**Deliberately omit:**

- `pip freeze` output. It includes every transitive dependency and can reveal
  package names, versions and environment internals irrelevant to the parser.
- Full production `manifest.json` or compiled SQL from a real warehouse. Compiled
  files carry warehouse object names, schema details and sometimes SQL that was
  injected from credentials; a full manifest includes nodes you did not need.
  Ask instead for a minimized, invented case — the same bug almost always shows
  up in a five-line SELECT on `orders`.

## What a minimized case must keep

A SQL-only `extract_columns` reproduction cannot prove a verdict or a cascade
finding, because those are computed from more than one model's SQL:

- **Dependencies.** A `broken_ref` requires the downstream model that reads the
  dropped column. Trimming it away makes the cascade disappear.
- **Policies.** `on_schema_change` and the materialization decide the verdict;
  remove them and the same column change is just "safe" or "unknown".
- **Manifest identity.** The resolver attributes reads to the changed relation
  using manifest lineage and node identity. A hand-copied SQL snippet loses that
  link, so it cannot demonstrate a `READS_DROPPED` impact.

When your bug is in the verdict or the lineage, keep the small baseline/current
pair complete — both manifests (or at least the relevant nodes), both model SQL
files, and the configuration that drives the risk rule. Strip names and values to
the bookstore domain, but keep the shape that carries the bug.

## Reproduce from anywhere

Every snippet above was executed with resolved absolute fixture paths and
produced identical output from the repository root and from an unrelated working
directory. Put the same care into a reproduction: relative paths that depend on a
particular `cd` are the most common thing that makes a "minimal" case not
reproducible on the maintainer's machine. If a bug needs a specific working
directory, say so explicitly in the report.