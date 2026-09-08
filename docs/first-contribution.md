# Your first contribution to dbt-plan

Start with a small task whose behavior is already agreed. The tasks below use
committed SQL and manifests: **no warehouse account or credentials are needed**.
You can contribute documentation, SQL examples, or a small Bash/Python fix.

Read [CONTRIBUTING](../CONTRIBUTING.md) for setup and review rules, and
[CLAUDE.md](../CLAUDE.md) before changing production code. When analysis cannot
determine safety, it must report uncertainty for review; never turn a parse
failure into an all-clear.

## Choose one task

This is the September 2026 starter batch. **Check each issue's current status,
assignee and recent comments before starting.** The
[live open starter list](https://github.com/PresentJay/dbt-plan/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
is the source of truth; a link here is not a promise that the task is unclaimed.

For a first visit, try **#206** if you prefer documentation, **#201** if you know
PostgreSQL, or **#204** if you know DuckDB. Choose one; there is no race to finish
all of them.

| Task | Useful familiarity | Files / finish line |
|---|---|---|
| [#201 DISTINCT ON](https://github.com/PresentJay/dbt-plan/issues/201) | PostgreSQL, basic pytest | One new SQL fixture and test module; exact output names plus an alias control. |
| [#202 Aggregate FILTER](https://github.com/PresentJay/dbt-plan/issues/202) | PostgreSQL, basic pytest | One new fixture and test module; filter inputs never become output columns. |
| [#203 Named WINDOW](https://github.com/PresentJay/dbt-plan/issues/203) | Window functions, basic pytest | One new fixture and test module; renaming the window preserves output names. |
| [#204 GROUP BY ALL](https://github.com/PresentJay/dbt-plan/issues/204) | DuckDB, basic pytest | One new fixture and test module; grouping produces no wildcard output. |
| [#205 UNNEST WITH OFFSET](https://github.com/PresentJay/dbt-plan/issues/205) | BigQuery arrays, basic pytest | One new fixture and test module; exact ordered outputs and an offset-alias control. |
| [#206 Terminal output settings](https://github.com/PresentJay/dbt-plan/issues/206) | CLI usage, Markdown | A new section in `docs/configuration.md`; defaults, examples and precedence. |
| [#207 Reading the stats report](https://github.com/PresentJay/dbt-plan/issues/207) | CLI usage, Markdown | New `docs/stats.md`; a reproducible example and limits of the counts. |
| [#208 Sample runner errors](https://github.com/PresentJay/dbt-plan/issues/208) | Bash and Python subprocess tests | Sample runner plus one test module; execution errors stop the demo. |

Each issue specifies the exact files, expected result, control cases and commands.
The five SQL tasks preserve behavior already checked on SQLGlot 28.0.0 and
30.17.0 at `f15827b`; they do not require a parser feature or a live warehouse.
If the example stops matching on current `main`, report it before changing the
assertion. Passing a dialect fixture is not certification of that adapter.

[The fixture inventory (#4)](https://github.com/PresentJay/dbt-plan/issues/4)
also has an earlier recursive CTE request. That request remains separate from
this batch; coordinate there before proposing a recursive CTE patch.

## Claim, reproduce, then open a draft

1. Comment on one issue with the task and files you intend to change. A maintainer
   will confirm ownership after checking earlier requests. Questions in Korean
   or English are welcome; do not open competing PRs while ownership is unclear.
2. Fork the repository, clone your fork and branch from current `main`.
3. Install the test dependencies and run the issue's reproduction. For these
   tasks, `uv sync --extra test` is enough; local DuckDB integration tests need
   the additional setup in [CONTRIBUTING](../CONTRIBUTING.md#development-setup).
4. For a bug, write the failing test first. For a working SQL pattern, write the
   regression assertion first, then its fixture. Documentation work should be
   checked by running its examples and previewing links.
5. Open a draft PR early with `Fixes #<number>`, what changed, the command results
   and any questions. Keep to the issue's agreed files. Run its focused checks,
   then `make test` and `make lint` before requesting review; report local skips.

A maintainer aims to acknowledge a claim or question within **two business days**;
this is a capacity goal, not a guaranteed response time. If ownership is still
unclear, one follow-up on the issue is enough. You can ask to pause or release a
claim. Maintainers should check with an earlier claimant before reopening the
task to others.

Earlier valid work takes priority when PRs overlap. Correctness still decides
what can merge: if a later patch is selected, the review should explain the
specific gap in the earlier one and credit useful work from both contributors.

AI assistance is allowed under the
[run it and understand it rule](../CONTRIBUTING.md#using-an-ai-assistant).
You should be able to explain the result and its safety limits.

## Try a local report first

After `uv sync --extra test`, run this from the repository root:

```bash
uv run --no-sync dbt-plan check \
  --base-dir "$PWD/examples/sample-project/base" \
  --project-dir "$PWD/examples/sample-project/current" \
  --format json
```

This uses checked-in compiled artifacts. It does not compile a project or connect
to a warehouse. At `f15827b`, it checks four changed models, reports three safe and
one destructive, and records one cascade risk. `int_order_enriched` drops
`shipping_info` and `billing_info`. **Exit code 1 is the expected destructive
finding**, not a setup failure. The JSON is useful even though the exit is nonzero.

This is a contributor demo from `main`, not a live warehouse test or a promise
that an older PyPI release has the same behavior. See the
[exit-code contract](exit-codes.md) for the release boundary. The optional
`dbt-plan run` workflow invokes a compile command and has different setup needs.

## Check your PR's CI

The repository bot keeps one status comment with the tested commit, approval
waits and failed-job links. Comment `/ci` to refresh it. Use `/ci retry` for a
transient failure after a run has executed; fix code failures and push a commit.
An external commit may wait for a maintainer's execution approval. That wait is
not a test failure, and retry cannot bypass it.

See [CI commands and limits](../CONTRIBUTING.md#checking-ci-on-your-pull-request).
Maintainers review the exact change and required CI before merging. Stars,
promotional posts and recruiting other contributors are never conditions of
getting help or having a contribution accepted.
