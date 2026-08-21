# dbt-plan

`dbt run` 전에 위험한 DDL 변경을 경고하는 정적 분석 도구.

dbt 버전의 `terraform plan`. Warehouse 접속 없이 동작. 모든 warehouse 지원 (Snowflake, BigQuery, Redshift, Postgres 등).

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

### 더 많은 명령

```bash
dbt-plan init              # .dbt-plan.yml 설정 파일 생성
dbt-plan stats             # 프로젝트 분석
dbt-plan ci-setup          # GitHub Actions 워크플로우 생성
dbt-plan check --format github   # GitHub 마크다운 출력
dbt-plan check --format json     # CI 파이프라인용 JSON
dbt-plan check --select model1   # 특정 모델만 체크
```

## 출력 예시

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

## 현재 되는 것 (v0.4.1)

| 기능 | 상태 | 설명 |
|------|------|------|
| 컬럼 추출 (SQLGlot) | **완료** | 멀티 dialect (Snowflake, BigQuery, Postgres 등) |
| DDL 위험도 판정 | **완료** | materialization × on_schema_change 전체 조합 |
| Cascade 영향 분석 | **완료** | Broken ref, build failure 감지 |
| 설정 변경 감지 | **완료** | materialization, on_schema_change 정책 변경 |
| 삭제된 모델 감지 | **완료** | 항상 DESTRUCTIVE (ephemeral = SAFE) |
| 파싱 실패 안전 | **완료** | 컬럼 추출 실패 시 절대 SAFE 반환 안 함 |
| 중복 컬럼 안전 | **완료** | 모호한 컬럼 → REVIEW REQUIRED |
| SELECT * fallback | **완료** | manifest 컬럼 정의로 대체 |
| 출력 포맷 | **완료** | `--format text` (컬러) / `github` / `json` |
| 설정 시스템 | **완료** | `.dbt-plan.yml` + 환경변수 (`DBT_PLAN_*`) + `compile_command` |
| 명령어 | **완료** | `snapshot`, `check`, `init`, `stats`, `run`, `ci-setup` |
| 원커맨드 체크 | **완료** | `dbt-plan run` — 컴파일 + 스냅샷 + 체크 한번에 |
| CI 설정 | **완료** | `dbt-plan ci-setup` — GitHub Actions 워크플로우 생성 |
| 모델 필터링 | **완료** | `--select` / `ignore_models` |
| CI 통합 | **완료** | 1141 tests, 98% coverage |

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

- dbt-core 1.7+
- 모든 warehouse: Snowflake, BigQuery, Redshift, Postgres, DuckDB 등 (`--dialect`)
- Python 3.10+
- CTE, UNION ALL, QUALIFY, 윈도우 함수, VARIANT 접근

## 라이선스

Apache-2.0
