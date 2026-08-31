# AGENTS.md

Guidance for coding agents working **on dbt-plan itself**. If you are working on a dbt
project that *uses* dbt-plan, run `dbt-plan agent-setup` in that repo instead — it writes
the consumer-facing version of this file.

The full instructions live in **[CLAUDE.md](CLAUDE.md)** (identity, scope boundaries,
architecture, and the DDL prediction rules) and **[CONTRIBUTING.md](CONTRIBUTING.md)**
(setup, tests, how to add a feature). Read CLAUDE.md before changing anything under
`src/dbt_plan/`. This file deliberately does not restate those rules — a transcribed copy
drifts from the original, which is a bug this repo has already had to fix once.

The one invariant worth repeating, because everything else follows from it:

> When dbt-plan cannot determine whether a change is safe, it reports a warning, never
> "safe". A false warning is acceptable. A false all-clear is the failure this tool exists
> to prevent.

Concretely: never return `safe` on a parse failure. Return `None` and let the caller
escalate to review.

```bash
uv sync --extra test
make test      # full suite
make lint      # ruff
```

Every feature needs tests before implementation.
