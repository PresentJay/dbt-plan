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
| `--format` | `text` | 출력 포맷 (`text` / `github`) |

### `dbt-plan --version`

```bash
dbt-plan --version    # dbt-plan 0.1.0
```

## Exit Codes

| Code | Safety | Description | CI 동작 |
|------|--------|-------------|---------|
| 0 | SAFE | 안전한 변경 (CREATE OR REPLACE, ADD COLUMN) | 통과 |
| 1 | DESTRUCTIVE | 파괴적 변경 (DROP COLUMN, MODEL REMOVED) | merge 차단 |
| 2 | WARNING | 파싱 실패 또는 인프라 오류 | 통과 (경고) |

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
