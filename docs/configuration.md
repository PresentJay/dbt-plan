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
