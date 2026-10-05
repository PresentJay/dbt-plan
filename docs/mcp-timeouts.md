# MCP plan timeouts

The MCP server limits each `plan` analysis subprocess to **120 seconds** by
default. Set `DBT_PLAN_MCP_PLAN_TIMEOUT_SECONDS` in the MCP server's environment
to override it, for example:

```sh
DBT_PLAN_MCP_PLAN_TIMEOUT_SECONDS=300 dbt-plan-mcp
```

For a desktop MCP client, set this variable in the server entry's `env` mapping
and restart the server. The value must contain only ASCII decimal digits,
representing an integer from **1 through 3600**, inclusive. Leading zeros are
accepted. An unset
variable uses 120; an empty value, whitespace, signs, fractions, or an out-of-range
value is invalid. The server reads the setting on each plan call.

Invalid settings return a tool result before any child is launched:

```json
{
  "verdict": "error",
  "reason": "configuration_error",
  "error": "DBT_PLAN_MCP_PLAN_TIMEOUT_SECONDS must be an integer from 1 through 3600 (seconds)"
}
```

If the analysis exceeds the limit, the server kills and reaps its direct child
before returning:

```json
{
  "verdict": "error",
  "reason": "timeout",
  "timeout_seconds": 120,
  "error": "dbt-plan analysis timed out after 120 seconds"
}
```

A timeout is an execution error, never a safety verdict. All captured output is
discarded, even if it contains valid JSON: an unfinished process has not delivered
a completed analysis. No partial summary, models, or exit code is returned, and
the handler prints nothing to MCP stdout. Check local filesystem availability or
raise the limit within the supported range, then retry `plan`. Do not treat a
timeout as permission to run dbt.

Completed calls retain UTF-8 decoding and the existing report validation,
refusals, and safe/destructive/review-required verdicts. The timeout bounds the
subprocess wait, not total request latency: process creation and OS cleanup can
take additional time. This is not cancellation of arbitrary external process
trees; the analysis CLI does not launch such a tree.

The limit applies only to `plan` (`dbt-plan check`). It does not limit `snapshot`,
which mutates the baseline and needs separate recovery semantics, or the standalone
CLI. This feature adds no compile tool, warehouse access, or runtime dependency.

To verify the behavior with the optional MCP dependencies installed:

```sh
uv sync --extra test --extra dev --extra mcp
uv run --no-sync pytest tests/test_mcp_plan_timeout.py tests/test_mcp_report_validation.py tests/test_mcp_server.py -q
```

The timeout tests use mocked partial output and one real temporary Python child
blocked on a pipe, with an internal 0.05-second limit. They check child reaping
without waiting for the production limit or relying on a long sleep.
