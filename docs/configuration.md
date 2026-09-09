# Configuration Reference

## CLI Commands

### `dbt-plan snapshot`

현재 compiled SQL + manifest.json을 기준선으로 저장합니다.

```bash
dbt-plan snapshot [--project-dir DIR] [--target-dir DIR]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--project-dir` | `.` | dbt 프로젝트 루트 디렉토리 |
| `--target-dir` | `target` | dbt compile 출력 디렉토리 |

저장 경로: `{project-dir}/.dbt-plan/base/`
- `base/compiled/` — compiled SQL 파일
- `base/manifest.json` — manifest 사본

### `dbt-plan check`

base(snapshot)와 current(target)를 비교하여 DDL 영향을 예측합니다.

```bash
dbt-plan check [--project-dir DIR] [--target-dir DIR] [--base-dir DIR] [--manifest PATH] [--format FORMAT]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--project-dir` | `.` | dbt 프로젝트 루트 디렉토리 |
| `--target-dir` | `target` | dbt compile 출력 디렉토리 |
| `--base-dir` | `.dbt-plan/base` | snapshot 디렉토리 |
| `--manifest` | `{target-dir}/manifest.json` | manifest.json 경로 |
| `--format` | `text` | 출력 포맷 (`text` / `github` / `json`) |
| `--acknowledge` | (없음) | 검토를 마친 파괴적 변경 모델 (쉼표 구분) |

#### 파괴적 변경 승인 (`--acknowledge`)

의도한 `DROP COLUMN` 을 머지하려고 체크 자체를 끄는 대신, 해당 모델만 명시적으로 승인합니다.
`ignore_models` 와 달리 **출력에서 사라지지 않습니다** — `[ACKNOWLEDGED]` 로 표시되고 요약에도
따로 집계되며, exit code에만 반영되지 않습니다.

```bash
dbt-plan check --acknowledge int_order_enriched
DBT_PLAN_ACKNOWLEDGE=int_order_enriched dbt-plan check
```

```yaml
# .dbt-plan.yml
acknowledge_models: [int_order_enriched]
```

모델명을 하나하나 적어야 하며 "전체 승인" 옵션은 의도적으로 없습니다. 라벨 하나로 모든
destructive를 통과시키면, 리뷰어가 승인한 변경 말고 나중에 섞여 들어온 변경까지 조용히
빠져나가기 때문입니다. 승인하지 않은 다른 모델의 위험, 관련 없는 warning, 파싱 실패는
그대로 빌드를 실패시킵니다.

GitHub Actions에서 PR 라벨과 연동하려면 워크플로가 라벨을 읽어 env로 넘겨줍니다
(dbt-plan 자체는 GitHub을 알지 못합니다):

```yaml
- run: dbt-plan check
  env:
    DBT_PLAN_ACKNOWLEDGE: ${{ contains(github.event.pull_request.labels.*.name, 'ddl-reviewed') && needs.detect.outputs.models || '' }}
```

### `dbt-plan run`

Compiles the baseline and current state, then checks the DDL impact. `--target-dir` is passed to
both the baseline snapshot and the final check for projects that use a non-default output
directory in `dbt_project.yml` or dbt configuration.

```bash
dbt-plan run [--project-dir DIR] [--target-dir DIR] [--against REF]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--project-dir` | `.` | dbt project root directory |
| `--target-dir` | `target` | dbt compile output directory |
| `--against` | (last commit) | Compare with where the branch diverged from the ref |

When compiled SQL is missing, the error message shows the actual target directory that was searched.

### `dbt-plan --version`

```bash
dbt-plan --version    # dbt-plan 0.1.0
```

## Terminal output settings

Three settings control how dbt-plan renders a report: the output `format`,
colored terminal output, and `verbose` diagnostics. Defaults are `format: text`,
`no_color: false`, `verbose: false`. None of them change what is decided — only
how the decision is shown.

### `format`

`format` accepts one of three values:

| Value    | Meaning                                                       |
|----------|---------------------------------------------------------------|
| `text`   | Human-readable terminal output (default).                     |
| `github` | GitHub-flavored Markdown with status icons.                   |
| `json`   | Machine-readable JSON report (example later in the JSON section). |

### `no_color`

Colored terminal output is on by default only when stdout is a terminal, and is
**automatically disabled when output is piped** (for example `dbt-plan check | tee report.txt`).
`no_color: true` disables colors even on an interactive terminal. There is no
`--color` flag that forces colors back on.

### `verbose`

`verbose: true` prints diagnostic details (directories, columns, parse skips) to
stderr. It is the first thing to turn on when a report does not match what you
expected.

### Where each setting can be set

Every setting has a config-file key, an environment variable, and a `check`
flag. Precedence is the usual one: CLI flag > environment variable > config file > default.

| Setting  | Config file key | Environment variable | `check` flag  |
|----------|-----------------|----------------------|---------------|
| format   | `format`        | `DBT_PLAN_FORMAT`    | `--format`    |
| no_color | `no_color`      | `DBT_PLAN_NO_COLOR`  | `--no-color`  |
| verbose  | `verbose`       | `DBT_PLAN_VERBOSE`   | `--verbose`   |

```yaml
# .dbt-plan.yml
format: github
no_color: false
verbose: false
```

```bash
DBT_PLAN_FORMAT=json dbt-plan check
```

The JSON environment invocation above runs the checked-in sample project demo
described in the
[first-contribution guide](first-contribution.md); its destructive finding
intentionally exits 1, and the JSON is still usable (see
[the exit-code contract](exit-codes.md)).

### Precedence rules

`format` follows the usual precedence exactly: an explicit `--format` flag beats
`DBT_PLAN_FORMAT`, which beats the config file, which beats the `text` default.
The environment variable is applied on top of the file, so `DBT_PLAN_FORMAT`
overrides a file `format` (including an explicit `format: text`).

The boolean settings read from the environment have a deliberate asymmetry: only
truthy values (`true`, `1`, `yes`, case-insensitive) enable them.
`DBT_PLAN_NO_COLOR=false` does **not** undo a config file that sets
`no_color: true`, and `DBT_PLAN_VERBOSE=false` does not disable a file
`verbose: true`. Turning a file-level `true` back off means editing the config
file.

CLI boolean flags only enable behavior: `--no-color` and `--verbose` turn the
corresponding feature on even when the config file or environment left it off.
There is no `--color` or `--no-verbose` override to turn them off from the
command line. In the config file, `no_color` and `verbose` accept a boolean
spelling directly (`true` / `1` / `yes`, and `false` / `0` / `no`,
case-insensitive).

## Exit Codes

| Code | Safety | Description | CI 동작 |
|------|--------|-------------|---------|
| 0 | SAFE | 안전한 변경 (CREATE OR REPLACE, ADD COLUMN) | 통과 |
| 1 | DESTRUCTIVE | 파괴적 변경 (DROP COLUMN, MODEL REMOVED) | merge 차단 |
| 2 | WARNING | SQL 분석의 불확실성 또는 잠재적 빌드 실패 | 기본 CLI에서는 실패; CI 정책에 따라 허용 |
| 3 | ERROR | 입력·컴파일·복원·내부 오류로 실행을 완료하지 못함 | 차단 |

다음 마이너 릴리스부터 적용하는 변경입니다. `warning_exit_code`의 기본값은 2이며,
실행 오류에 쓰는 3은 경고 코드로 설정할 수 없습니다. [이전 버전에서 전환하기](exit-codes.md)를 참고하세요.

## 디렉토리 구조

dbt-plan이 기대하는 dbt 프로젝트 구조:

```
my-dbt-project/
├── target/
│   ├── compiled/{project_name}/models/**/*.sql
│   └── manifest.json
├── .dbt-plan/
│   └── base/                    # dbt-plan snapshot이 생성
│       ├── compiled/**/*.sql
│       └── manifest.json
└── .gitignore                   # .dbt-plan/ 추가 권장
```

`.gitignore`에 추가:
```
.dbt-plan/
```

## 출력 포맷

### Text (터미널)

```
dbt-plan -- 2 model(s) changed

DESTRUCTIVE  int_order_enriched (incremental, sync_all_columns)
  DROP COLUMN  shipping_info
  ADD COLUMN   shipping_city
  Downstream: dim_customers (1 model(s))

SAFE  dim_customers (table)
  CREATE OR REPLACE TABLE
```

### GitHub Markdown

```markdown
### dbt-plan -- 2 model(s) changed

🔴 **DESTRUCTIVE** `int_order_enriched` (incremental, sync_all_columns)
- `DROP COLUMN` shipping_info
- `ADD COLUMN` shipping_city
- Downstream: dim_customers (1 model(s))

✅ **SAFE** `dim_customers` (table)
- CREATE OR REPLACE TABLE
```

### JSON

Use `--format json` when another program needs the complete result. This example
was produced by snapshotting the fixture project, removing `customer_id` from
the compiled `stg_orders` model, and running `dbt-plan check --format json`:

```json
{
  "summary": {
    "total": 1,
    "safe": 0,
    "warning": 1,
    "destructive": 0,
    "cascade_risks": 5
  },
  "models": [
    {
      "model_name": "stg_orders",
      "materialization": "view",
      "on_schema_change": null,
      "safety": "warning",
      "operations": [
        {
          "operation": "CREATE OR REPLACE VIEW",
          "column": null
        }
      ],
      "columns_added": [],
      "columns_removed": [],
      "acknowledged": false,
      "downstream": ["dim_books", "fct_orders"],
      "downstream_exposures": [
        {
          "name": "orders_dashboard",
          "type": "dashboard",
          "owner": "Data Team <data@example.com>",
          "url": "https://example.com/dashboards/orders"
        }
      ],
      "downstream_impacts": [
        {
          "model_name": "test_stg_orders_shape",
          "risk": "unit_test_failure",
          "reason": "expect names dropped column(s): customer_id"
        },
        {
          "model_name": "test_dim_books_groups_by_store",
          "risk": "unit_test_failure",
          "reason": "given for stg_orders names dropped column(s): customer_id"
        },
        {
          "model_name": "accepted_values_stg_orders_customer_id__cust_abc",
          "risk": "data_test_failure",
          "reason": "tests dropped column(s): customer_id"
        },
        {
          "model_name": "no_order_without_a_customer",
          "risk": "data_test_failure",
          "reason": "its SQL names dropped column(s): customer_id"
        },
        {
          "model_name": "not_null_stg_orders_customer_id",
          "risk": "data_test_failure",
          "reason": "tests dropped column(s): customer_id"
        }
      ]
    }
  ],
  "parse_failures": [],
  "stale_sources": [],
  "skipped_models": [],
  "uncompiled_models": []
}
```

#### Field reference

The top-level keys below are always present. Their arrays are empty when there are no
matching findings.

| Field | Type | Meaning |
|------|------|---------|
| `summary` | object | Counts for the complete check. |
| `models` | array | One entry per changed model. |
| `parse_failures` | string array | Models whose compiled SQL could not be parsed. |
| `stale_sources` | string array | Source or compiled inputs whose freshness/compilation could not be established. |
| `skipped_models` | string array | Compiled models not found in the manifest. |
| `uncompiled_models` | string array | Manifest models with no compiled SQL. |

An additional top-level `baseline_problem` string is present only when the
snapshot manifest is missing (`"missing"`) or cannot be read (`"corrupt"`).
Deleted-model and baseline-configuration checks cannot be trusted in that state;
recreate the snapshot from the intended baseline revision before relying on the
report. The field is omitted when the baseline is readable.

Refusal fields must be inspected even when `summary.total` is zero or a configured
`warning_exit_code: 0` makes the command exit successfully. No changed models is
not evidence that the whole project was assessed. The MCP wrapper reports a
baseline problem in `refusals` with `reason: "baseline_problem"` and the problem
value in `detail`; it cannot return `safe` while that refusal remains.

`summary.total`, `safe`, `warning`, and `destructive` are always present as
integers. `summary.acknowledged` appears only when at least one finding was
acknowledged, and `summary.cascade_risks` appears only when at least one cascade
risk exists.

Every model has these fields:

| Field | Type | Meaning |
|------|------|---------|
| `model_name` | string | Compiled model identifier. |
| `materialization` | string | dbt materialization reported by the manifest. |
| `on_schema_change` | string or null | Explicit schema-change policy, or null when absent. |
| `safety` | string | `safe`, `warning`, or `destructive`. |
| `operations` | array | Predicted operations, each with `operation` and nullable `column`. |
| `columns_added` | string array | Added columns recorded by the prediction; not a complete SQL diff for every materialization. |
| `columns_removed` | string array | Removed columns recorded by the prediction; not a complete SQL diff for every materialization. |
| `acknowledged` | boolean | Whether this model was explicitly acknowledged. |

For example, `table` and `view` replacements can leave both column arrays empty
while downstream findings still identify a removed SQL column, as in the example
above. Use the operations, final safety, and downstream impacts together.

The following model fields are omitted, rather than set to null or an empty
array, when there is no data:

| Field | Item shape | Meaning |
|------|------------|---------|
| `downstream` | string | Reachable downstream model name. |
| `downstream_exposures` | `name`, `type`, `owner`, `url` | Exposure that depends on the changed model; unavailable owner or URL values are empty strings. |
| `downstream_impacts` | `model_name`, `risk`, `reason` | Predicted cascade finding. |

Required arrays use `[]` when empty. The only null values in the model contract
are an absent `on_schema_change` and an operation without a column.

#### Cascade risk vocabulary

| Risk | Severity | Meaning |
|------|----------|---------|
| `broken_ref` | destructive | Downstream SQL reads or references a removed column. |
| `contract_violation` | warning | A downstream enforced contract fails or its SQL columns cannot be established. |
| `build_failure` | warning | An incremental downstream model uses `on_schema_change=fail` after an upstream schema change. |
| `unit_test_failure` | warning | A unit-test fixture names a removed column. |
| `unit_test_unreadable` | warning | A unit-test fixture cannot be inspected well enough to decide. |
| `inherited_drop` | destructive | An unchanged downstream model inherits a column loss and its configuration predicts a destructive operation. |
| `inherited_change` | warning | An unchanged downstream model inherits a change that requires review. |
| `data_test_failure` | warning | A generic or singular data test reads a removed column. |
| `data_test_unreadable` | warning | A data test cannot be inspected well enough to decide. |

Consumers should use `models[].safety` for the final severity and tolerate new
`risk` strings in minor releases. Treat an unknown risk as a warning that needs
review; never interpret it as safe.

### Analysis inputs

CLI JSON reports include `analysis`: `dialect`, `dialect_source`, `adapter_type`,
`baseline` (`revision`, `created_at`, and, for new snapshots, `dbt_plan_version`),
`selection`, and `unsupported_languages`. Missing legacy provenance is explicitly
`null`; the report does not guess a revision. Git revision records the checkout
where the snapshot was taken, not proof that compiled artifacts came from a clean commit.
Text and GitHub output show the dialect and baseline even when there are no changes.

`agent-setup --file CLAUDE.md` writes to an explicit instruction file instead of
`AGENTS.md`; nested paths such as `.cursor/rules/dbt-plan.mdc` work too. Existing
instructions are preserved and a duplicate dbt-plan section is refused.

Selection supports model names and leading/trailing `+`. Unsupported operators
or unknown names are execution errors, never an empty successful check. Missing
or stale models outside the selection and its upstream/downstream graph do not
block that selection; shared macro, project, and schema-file uncertainty still does.
An unreadable baseline remains a project-wide refusal.
