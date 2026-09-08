# dbt-plan

`dbt run` 전에 위험한 DDL 변경을 경고하는 정적 분석 도구.

dbt 버전의 `terraform plan`. 컴파일된 SQL로 동작 — `dbt compile`은 접속이 필요하지만, 그 다음부터 dbt-plan은 파일만 읽습니다. 모든 warehouse 지원 (Snowflake, BigQuery, Redshift, Postgres 등).

## 어떻게 보이는가

```text
$ dbt-plan check

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

## 무엇을 하는가

PR에서 dbt 모델이 변경되었을 때, 컴파일된 SQL 비교로:

- **컬럼 변경 감지**: ADD/DROP COLUMN
- **위험도 판정**: materialization × on_schema_change 규칙 기반
- **하위 모델 영향 분석**: 삭제된 컬럼을 참조하는 downstream 모델 감지
- **타입 변경 감지**: 명시적 `CAST`의 타입을 비교하고, 한쪽에만 CAST가 생기거나 사라져도 검토 요청
- **설정 변경 감지**: materialization/on_schema_change 정책과 database/schema/alias 이동

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
| incremental | ignore | 스키마 DDL 없음; 기존 테이블의 컬럼 변경은 빌드 실패 가능 | 컬럼 변경·분석 불가 시 WARNING |
| incremental | fail | 빌드 실패 | WARNING |
| incremental | append_new_columns | `ADD COLUMN`만 | SAFE |
| incremental | sync_all_columns | `ADD + DROP COLUMN` | 컬럼 삭제 시 DESTRUCTIVE |
| 전체 | (모델 삭제) | MODEL REMOVED | DESTRUCTIVE |
| 전체 | (알 수 없는 osc) | `UNKNOWN on_schema_change` | WARNING |
| materialized_view / 커스텀 | (미설정) | `UNKNOWN materialization` | WARNING |
| materialized_view / 커스텀 | (osc 설정됨) | incremental 규칙을 따름 | osc에 따름 |

## 지원 환경

- dbt-core 1.7+, dbt Fusion 엔진 (`2.0.0-preview.218`로 검증)
- 모든 warehouse: Snowflake, BigQuery, Redshift, Postgres, DuckDB 등 (`--dialect`)
- Python 3.10+
- CTE, UNION ALL, QUALIFY, 윈도우 함수, VARIANT 접근

## 기여하기

개발 환경과 기여 절차는 [CONTRIBUTING.md](CONTRIBUTING.md)를 참고하세요.

**기여자 CI 안내:** 이 저장소에 PR을 열면 봇이 하나의 상태 댓글을 갱신해
검사 대상 커밋, 실행 승인 대기 여부, 실패한 작업 링크를 알려줍니다.
`/ci` 댓글로 상태를 새로고침하고, 일시적인 실행 오류는 `/ci retry`로 재시도할 수 있습니다.
외부 기여의 실행 승인은 여전히 관리자가 처리하며, 재시도로 우회할 수 없습니다.
[명령과 제한 사항](CONTRIBUTING.md#checking-ci-on-your-pull-request)을 확인하세요.
이 기능은 dbt-plan 자체에 기여하는 PR용이며, 사용자 dbt 프로젝트에 설치하는 Action과는 별개입니다.

## 라이선스

Apache-2.0
