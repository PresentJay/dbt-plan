# Config file syntax

dbt-plan reads options from `.dbt-plan.yml` in the selected project directory
(`--project-dir`, default `.`). The parser in `Config.load`
(`src/dbt_plan/config.py`) is a **small custom parser, not a general YAML
reader**: a handful of top-level keys with scalar or simple list values are
supported, and everything else is either warned about on stderr or ignored.
This guide describes the syntax you can rely on. The full range of settings and
their precedence live in the [configuration reference](configuration.md).

## Scalar settings

Values are `key: value` on one line. Quotes are optional for most values; a
quoted scalar has its surrounding single or double quotes stripped.

```yaml
format: "json" # report format
no_color: yes
```

A `#` starts a trailing comment only when it is preceded by whitespace and is
outside quotes, so `format: "json" # report format` parses `format` as `json`
with the comment discarded. Comments are stripped before a value is parsed.

Booleans are spelled with words or digits, case-insensitively: `true` / `1` /
`yes` turn an option on, `false` / `0` / `no` turn it off. `format` accepts one
of `text`, `github` or `json`; anything else is warned about and ignored, and so
is an unrecognized key, so a misspelled setting never silently becomes a
different one.

File-level `verbose` is supported on current `main` (the fix landed in #229).
Published releases from before that change may not apply a file-level `verbose`;
use `DBT_PLAN_VERBOSE=1` or the `--verbose` flag on the version you have until
you upgrade.

## Model-name lists

`ignore_models` and `acknowledge_models` accept a model-name list in any of
three equivalent spellings.

```yaml
# Bracket list
ignore_models: [demo_orders, demo_customers]

# Comma-separated
ignore_models: demo_orders, demo_customers

# Indented block list
ignore_models:
  - demo_orders
  - "demo_customers"
```

All three forms produce the same two-name list. Quoted items are unquoted the
same way scalar values are (`"demo_customers"` becomes `demo_customers`). This
is a syntax guide only: the safety behavior of ignore/acknowledgement policies
is unchanged and the same matching rules as before apply.

## Reading it back

`Config.load` applies the file, then overlays `DBT_PLAN_*` environment
variables. With the environment cleared, the example above is reproduced like
this:

```python
import tempfile
from pathlib import Path

from dbt_plan.config import Config

root = Path(tempfile.mkdtemp())
(root / ".dbt-plan.yml").write_text(
    'format: "json" # report format\n'
    "no_color: yes\n"
    "ignore_models:\n"
    "  - demo_orders\n"
    '  - "demo_customers"\n',
    encoding="utf-8",
)

cfg = Config.load(root)
assert cfg.format == "json"
assert cfg.no_color is True
assert cfg.ignore_models == ["demo_orders", "demo_customers"]
```

Verified on current `main` with all `DBT_PLAN_*` variables removed: the example
yields `format json`, `no_color True`, and the two listed model names.

## Typos are warnings, not settings

A misspelled key is reported on stderr with the file path and line number and
does not set anything:

```yaml
formatt: json
```

produces

```
{project-dir}/.dbt-plan.yml:1: warning: cannot understand formatt
```

and `format` stays at its default `text`. A typo warning is not a silent
application of the requested setting: read the warnings on stderr and fix the
line before assuming the config took effect.

## What is not supported

Nested mappings, YAML anchors and aliases, block scalar syntax (`|`, `>`),
general YAML escaping inside values, and quoted keys are **not** part of the
supported contract. Notably, not all unsupported input is rejected cleanly:
unrecognized keys and malformed values are warned about and ignored, so do not
build on constructs outside this guide. No runtime code or YAML dependency is
involved — the parser is the source of truth, and its behavior is covered by
`tests/test_config.py`.

## See also

- [Configuration reference](configuration.md) — every setting, its environment
  variable and its CLI flag, plus the terminal-output settings section
- [CONTRIBUTING](../CONTRIBUTING.md) — running the project and its tests