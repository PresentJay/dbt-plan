# Exit codes and migration

This contract is implemented on main for the **next 0.x minor release**. It is a
breaking CI-interface change and must not be released as a 0.15.x patch. Existing
published versions and Action tags retain their behavior until upgraded.

| Default code | Meaning | Is there a completed check report? |
|---|---|---|
| 0 | No blocking findings under the configured policy | Yes |
| 1 | Destructive findings | Yes |
| 2 | Review required | Yes |
| 3 | Execution could not complete | No reliable completed verdict |

This table describes `check` and `run`. Setup commands and `--help`/`--version`
exit 0 on success without producing an analysis report. Invalid CLI arguments
also exit 3, including errors normally reported by argparse as 2.

A review finding includes unparseable or non-UTF-8 model SQL, unknown rules,
incomplete baselines, stale inputs, and potentially broken builds/tests that can
be represented in a report. A missing required directory, unreadable current
manifest/config, failed compile or recovery, or unexpected exception is an
execution failure. Fix the error on stderr and rerun. Do not accept stdout as a
completed report when the process failed, even if it wrote partial output first.

## Warning policy stays separate

The default remains `warning_exit_code: 2`. File/environment configuration and
acknowledgements keep their existing behavior. `warning_exit_code: 0` allows review
findings without hiding them from the report; execution failures still exit 3.
Other custom warning codes remain supported, **except 3**, which is now reserved.
The resolved configuration is checked after environment overrides. An unreadable
configuration fails instead of silently applying defaults.

The CLI does not yet provide `--fail-on` (#35). Its future default must describe
today's warning policy, and no policy option may suppress an execution failure.
The Action's `fail-on: destructive|warning|never` applies only after a completed
check; code 3 fails the Check step before the Gate step, even for `never`. The Action
also requires a JSON report with summary/model data before accepting a verdict, so
legacy package errors that exit 1 or 2 without a report still fail the step. The Action
continues to recognize standard verdict codes 0/1/2; custom warning codes outside
those values fail its Check step. Custom policies can change the exit status, so
inspect the report's findings rather than treating code 0 as proof of safety.

## Update consumers before upgrading

In 0.15.x, failures commonly exit 2 and uncaught Python exceptions exit 1. Scripts
that permit code 2 as a warning cannot reliably detect those failures. Pin the
existing package until ready, then upgrade package and Action references together.
For a shell consumer using standard warning codes, capture the command status
explicitly so `set -e` does not bypass policy handling:

```bash
code=0
dbt-plan check --format json > dbt-plan-report.json || code=$?
case "$code" in
  0) ;;                                      # passed the configured policy
  1) echo "Destructive findings" >&2; exit 1 ;;
  2) echo "Review required; inspect the report" >&2 ;;  # explicit allow-warning policy
  3) echo "Check failed; fix stderr errors and rerun" >&2; exit 3 ;;
  *) echo "Unexpected status: $code" >&2; exit "$code" ;;
esac
```

A bare CLI command still fails on any nonzero status. Newly generated `ci-setup`
workflows use an explicit `FAIL_ON: destructive` gate: they allow completed warnings
by default, and never allow execution errors. Report rendering cannot bypass that
gate. Existing workflow files are not rewritten by a package upgrade; regenerate
and review your project-specific settings to adopt this policy. See the
[CI guide](ci-integration.md) for dependency layouts and policy settings.
Update the dbt-plan section in consumer `AGENTS.md` after upgrading, or remove that
section and rerun `dbt-plan agent-setup` to regenerate it.

We chose a process code instead of only a JSON error envelope because shell
consumers need the distinction too. The JSON report schema is unchanged; errors
remain diagnostics on stderr. The next minor boundary follows the project's
pre-1.0 status ([Semantic Versioning](https://semver.org/spec/v2.0.0.html#spec-item-4)),
while this guide makes the compatibility change explicit.
