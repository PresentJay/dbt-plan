# dbt-plan

`dbt run` 전에 위험한 DDL 변경을 경고하는 정적 분석 도구.

dbt 버전의 `terraform plan`. 컴파일된 SQL로 동작 — `dbt compile`은 접속이 필요하지만, 그 다음부터 dbt-plan은 파일만 읽습니다. 모든 warehouse 지원 (Snowflake, BigQuery, Redshift, Postgres 등).

## 어떻게 보이는가

```text
$ dbt-plan check

dbt-plan -- 2 model(s) changed

DESTRUCTIVE  int_order_enriched (incremental, sync_all_columns)
  DROP COLUMN  shipping_info
  ADD COLUMN   shipping_city
  Downstream: dim_customers, fct_orders (2 model(s))
  >> BROKEN_REF  fct_orders: references dropped column(s): shipping_info

SAFE  dim_customers (table)
  CREATE OR REPLACE TABLE

dbt-plan: 2 checked, 1 safe, 0 warning, 1 destructive, 1 cascade risk(s)
```

## 무엇을 하는가

PR에서 dbt 모델이 변경되었을 때, 컴파일된 SQL 비교로:

- **컬럼 변경 감지**: ADD/DROP COLUMN
- **위험도 판정**: materialization × on_schema_change 규칙 기반
- **하위 모델 영향 분석**: 삭제된 컬럼을 참조하는 downstream 모델 감지
- **설정 변경 감지**: materialization/on_schema_change 정책 변경

실행하지 않습니다. Warehouse에 접속하지 않습니다. 파일을 읽고, 비교하고, 경고합니다.

## 빠른 시작

```bash
pip install dbt-plan

# dbt 프로젝트 디렉토리에서:
dbt-plan run               # 원커맨드: 컴파일 + 스냅샷 + 체크
```

`dbt compile`을 대신 실행해주므로, 평소 `dbt compile`에 쓰던 자격증명이 그대로 필요합니다. 로컬에서 컴파일이 안 되면 CI에서 돌리세요 (아래 참고) — dbt-plan은 거기서 나온 아티팩트를 읽습니다.

### 더 많은 명령

```bash
dbt-plan init              # .dbt-plan.yml 설정 파일 생성
dbt-plan stats             # 프로젝트 분석
dbt-plan ci-setup          # GitHub Actions 워크플로우 생성
dbt-plan agent-setup       # AGENTS.md 생성 — 코딩 에이전트에게 체크 실행법을 알려줌
dbt-plan check --format github   # GitHub 마크다운 출력
dbt-plan check --format json     # CI 파이프라인용 JSON
dbt-plan check --select model1   # 특정 모델만 체크
```



## 범위

| 범위 안 | 범위 밖 |
|---------|---------|
| 컬럼 ADD/DROP 감지 | `dbt run` 시뮬레이션 |
| materialization × osc 위험도 규칙 | Warehouse 접속 |
| Cascade broken ref / build failure | `seed` / `source` 변경 감지 |
| 설정 변경 감지 | `pre_hook` / `post_hook` DDL |
| CI exit codes + 구조화 출력 | `full_refresh` 모드 판정 |

**설계 원칙**: 거짓 경고는 괜찮고, 거짓 안전은 절대 안 됩니다.

## DDL 예측 규칙

| materialization | on_schema_change | 예측 DDL | 판정 |
|-----------------|------------------|----------|------|
| table | 무관 | `CREATE OR REPLACE TABLE` | SAFE |
| view | 무관 | `CREATE OR REPLACE VIEW` | SAFE |
| ephemeral | 무관 | (물리 오브젝트 없음) | SAFE |
| snapshot | 무관 | REVIEW REQUIRED | WARNING |
| incremental | ignore | DDL 없음 | SAFE |
| incremental | fail | 빌드 실패 | WARNING |
| incremental | append_new_columns | `ADD COLUMN`만 | SAFE |
| incremental | sync_all_columns | `ADD + DROP COLUMN` | 컬럼 삭제 시 DESTRUCTIVE |
| 전체 | (모델 삭제) | MODEL REMOVED | DESTRUCTIVE |

## 지원 환경

- dbt-core 1.7+, dbt Fusion 엔진 (`2.0.0-preview.218`로 검증)
- 모든 warehouse: Snowflake, BigQuery, Redshift, Postgres, DuckDB 등 (`--dialect`)
- Python 3.10+
- CTE, UNION ALL, QUALIFY, 윈도우 함수, VARIANT 접근

## 라이선스

Apache-2.0
