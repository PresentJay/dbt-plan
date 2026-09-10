# Developing dbt-plan on Windows

Build, test, and lint dbt-plan from source on Windows with a standard Python
installation and PowerShell 5.1 — no `make`, WSL, or Unix toolchain required.
The transcript at the bottom was recorded on a native Windows host (see the
Environment notes) and every command in it is exactly as run.

## Requirements

- Windows 10 or 11 with Windows PowerShell 5.1 (or PowerShell 7)
- [git](https://git-scm.com/)
- Python 3.10+ with the Windows **`py` launcher** (install from
  [python.org](https://www.python.org/downloads/windows/) and check `py -3 -V`).
  If your machine has `python` on PATH instead of the launcher, swap the venv
  command for `python -m venv .venv`; everything after it is identical.

Nothing in this guide requires the virtual environment **activation** script or
a change to the PowerShell **execution policy**: every Python command is
invoked through the explicit `.\.venv\Scripts\python.exe` (and
`.\.venv\Scripts\dbt-plan.exe`) path.

## Quickstart

Start from a clean copy of current `main` — the upstream repository or your own
fork — and create an isolated virtual environment:

```powershell
git clone https://github.com/PresentJay/dbt-plan.git
Set-Location dbt-plan

py -3 -m venv .venv              # no py launcher? Use: python -m venv .venv

.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

`-e` installs the project in editable mode, so edits under `src/` take effect
immediately. `.[test]` pulls the pytest extra used by CI. Then run an existing
test module to confirm the toolchain — `tests/test_columns.py` is a good
starter:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_columns.py -q
```

### Direct equivalents of the `make` targets

`make test`, `make lint`, `make format` and `make format-check` run the commands
defined directly in the repository's `Makefile`; `pyproject.toml` contributes
only the extras those commands need (`test` for pytest, `dev` for Ruff). The
same commands, run without `make`:

| `make …`        | PowerShell equivalent                                        |
|-----------------|--------------------------------------------------------------|
| `make test`     | `.\.venv\Scripts\python.exe -m pytest -v`                    |
| `make test-quick` | `.\.venv\Scripts\python.exe -m pytest -q`                  |
| `make lint`     | `.\.venv\Scripts\python.exe -m ruff check src\ tests\`       |
| `make format`   | `.\.venv\Scripts\python.exe -m ruff format src\ tests\`      |
| `make format-check` | `.\.venv\Scripts\python.exe -m ruff format --check src\ tests\` |

Ruff is **not** part of the test extra, so install the `dev` extra once before
running the lint/format targets:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Running the committed sample project

`examples\sample-project` needs no warehouse or dbt installation: the saved
baseline lives in `sample-project\base` and the "current" compiled project in
`sample-project\current`. PowerShell absolute paths work well with
`Resolve-Path` and `Join-Path`:

```powershell
$sample  = (Resolve-Path "examples\sample-project").Path
$base    = Join-Path $sample "base"
$current = Join-Path $sample "current"

.\.venv\Scripts\dbt-plan.exe check --base-dir $base --project-dir $current --format text
$LASTEXITCODE
```

The text report (colour codes omitted below; add `--no-color` when you pipe or
record output):

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

The check exits **1** because the sample drops columns on
`int_order_enriched`. Read `$LASTEXITCODE` **immediately** after a native
command: PowerShell keeps the exit status only until the next native command
completes. The codes follow the [exit codes and migration](exit-codes.md)
contract, which starts at **0.16.0**: `0` no blocking findings, `1`
destructive, `2` review required, `3` execution could not complete (no
completed-verdict report). Releases before 0.16.0 did not distinguish every
error this way, so pin the package and the Action together before upgrading.

## Skipped tests and optional integration

A test that needs a tool or extra the quickstart does not install **skips**
rather than fails. Everything else passes. For example,
`tests/test_dbt_e2e.py` skips without the `dbt` extra and a database adapter;
fixture-regeneration tests skip unless the pinned dbt-core version is present;
and the composite-Action shell tests skip when Bash is absent
(`shutil.which("bash")`):

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_compiled_fixture.py -rs -q
```

```text
.s                                                                       [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests\test_compiled_fixture.py:106: fixture regeneration needs dbt-core==1.11.7
1 passed, 1 skipped in 0.07s
```

`-rs` prints the reason next to each skip. **Report the skip counts you see**
when you ask about results — a green run with many skips has not exercised the
optional integration tiers, and the Windows CI matrix passing does not mean
those tiers run there. Setting up real dbt end-to-end tests (a target adapter,
profiles, dbt-core) is documented in [CONTRIBUTING](../CONTRIBUTING.md); it is
not claimed to work with only the test extra.

On the machine that recorded the transcript the full suite ends with the
environment-dependent failures that are expected there:

```text
FAILED tests/test_cli.py::TestSnapshotPathValidation::test_snapshot_rejects_base_dir_escaping_project
FAILED tests/test_cli.py::TestSnapshotPathValidation::test_snapshot_rejects_base_dir_resolving_to_project
FAILED tests/test_diff.py::TestSymlinkSkipping::test_symlink_sql_file_skipped
FAILED tests/test_exception_audit.py::TestDiffExceptionHandling::test_symlinks_skipped
FAILED tests/test_large_manifest.py::TestDiffCompiledDirsAtScale::test_diff_2000_files_100_modified
FAILED tests/test_large_manifest.py::TestDiffCompiledDirsAtScale::test_diff_mixed_added_removed_modified
FAILED tests/test_mutation_analysis.py::TestMutation10_RemoveSymlinkCheck::test_symlinks_must_be_skipped
7 failed, 1992 passed, 106 skipped in 94.60s (0:01:34)
```

The symlink tests fail with `OSError: [WinError 1314] A required privilege is
not held by the client` because creating links needs the SeCreateSymbolicLink
privilege (or Developer Mode / an elevated prompt), and the two
`test_large_manifest` tests time out at this machine's scale. These fail
identically on a pristine clone with no local edits. If you see a **different**
failure, report it — that is a real signal.

## Environment notes

The transcript below was recorded on an isolated clone of current `main`, on a
native Windows host:

- Windows PowerShell 5.1, `$PSVersionTable.PSVersion` = 5.1.26100
- OS report: `Microsoft Windows NT 10.0.26200.0`
- Python 3.14.5, installed without the `py` launcher; the recorded commands
  use the `python -m venv .venv` fallback shown in the quickstart
- sqlglot 30.18.0 and pytest 9.1.1 were resolved by pip at install time

The project's required Windows CI row runs this same `pytest` path on Python
3.10–3.14. Because `py` was not available on this host, venv creation in the
transcript uses `python`; every later command is the explicit
`.\.venv\Scripts\python.exe` path used in the quickstart, so the walkthrough
steps are identical.

## Native transcript

The profile folder is redacted to `PROFILE` (the issue's privacy rule for
user-specific paths). Pip's progress bars and wheel-cache paths are elided
with `...`, and terminal colour codes in the `check` report are omitted. All
commands and results are otherwise exactly as captured.

```text
PS C:\Users\PROFILE\work> git clone https://github.com/PresentJay/dbt-plan.git dbt-plan
Cloning into 'dbt-plan'...
...
PS C:\Users\PROFILE\work> Set-Location dbt-plan

PS C:\Users\PROFILE\work\dbt-plan> python -V        # py -3 -V when the launcher exists
Python 3.14.5

PS C:\Users\PROFILE\work\dbt-plan> python -m venv .venv

PS C:\Users\PROFILE\work\dbt-plan> .\.venv\Scripts\python.exe -V
Python 3.14.5

PS C:\Users\PROFILE\work\dbt-plan> .\.venv\Scripts\python.exe -m pip install -e ".[test]"
Obtaining file:///C:/Users/PROFILE/work/dbt-plan
  ... (build backend output elided)
Collecting sqlglot>=28.0.0 (from dbt-plan==0.16.0)
  ...
Successfully installed colorama-0.4.6 coverage-7.16.0 dbt-plan-0.16.0 iniconfig-2.3.0 packaging-26.3 pluggy-1.6.0 pygments-2.21.0 pytest-9.1.1 pytest-cov-7.1.0 sqlglot-30.18.0

PS C:\Users\PROFILE\work\dbt-plan> .\.venv\Scripts\python.exe -m pytest tests\test_columns.py -q
....................................                                     [100%]
36 passed in 0.36s

PS C:\Users\PROFILE\work\dbt-plan> .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Successfully installed dbt-plan-0.16.0 ruff-0.16.6

PS C:\Users\PROFILE\work\dbt-plan> .\.venv\Scripts\python.exe -m ruff check src\ tests\
All checks passed!

PS C:\Users\PROFILE\work\dbt-plan> .\.venv\Scripts\python.exe -m ruff format --check src\ tests\
100 files already formatted

PS C:\Users\PROFILE\work\dbt-plan> $sample  = (Resolve-Path "examples\sample-project").Path
PS C:\Users\PROFILE\work\dbt-plan> $base    = Join-Path $sample "base"
PS C:\Users\PROFILE\work\dbt-plan> $current = Join-Path $sample "current"
PS C:\Users\PROFILE\work\dbt-plan> $base
C:\Users\PROFILE\work\dbt-plan\examples\sample-project\base
PS C:\Users\PROFILE\work\dbt-plan> $current
C:\Users\PROFILE\work\dbt-plan\examples\sample-project\current

PS C:\Users\PROFILE\work\dbt-plan> .\.venv\Scripts\dbt-plan.exe check --base-dir $base --project-dir $current --format text
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

PS C:\Users\PROFILE\work\dbt-plan> $LASTEXITCODE
1

PS C:\Users\PROFILE\work\dbt-plan> .\.venv\Scripts\python.exe -m pytest tests\test_compiled_fixture.py -rs -q
.s                                                                       [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests\test_compiled_fixture.py:106: fixture regeneration needs dbt-core==1.11.7
1 passed, 1 skipped in 0.07s
```

## See also

- [First contribution](first-contribution.md)
- [CONTRIBUTING](../CONTRIBUTING.md) — setup, tests, and adding features;
  the `make` quickstart and real-dbt integration setup
- [Exit codes and migration](exit-codes.md) — the 0.16.0 exit-code contract
- [Configuration reference](configuration.md)