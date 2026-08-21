# dbt-plan

## Identity

dbt-plan은 `dbt run` 전에 위험을 경고하는 **정적 분석 도구**다.
실행하지 않는다. Warehouse에 접속하지 않는다. 경고한다.

`terraform plan`의 dbt 버전. 모든 warehouse 지원 (Snowflake, BigQuery, Redshift, Postgres 등).

## Core Principles

1. **놓치면 안 된다 (False Safe 금지)**
   - 파싱 실패 시 SAFE가 아니라 WARNING 반환 (None → REVIEW REQUIRED)
   - 컬럼 추출 불가 시 절대 safe 반환 금지

2. **오탐은 괜찮다 (False Warning 허용)**
   - 위험하지 않은 변경에 WARNING은 OK
   - 사용자가 `ignore_models`로 필터링 가능

3. **가볍고 빠르다 (No Runtime Dependency)**
   - sqlglot 외 런타임 의존성 없음
   - Warehouse 접속 불필요
   - 200개 모델 프로젝트에서 < 5초

4. **CI 친화적이다**
   - exit code로 판정 (0=safe, 1=destructive, 2=warning)
   - JSON/GitHub markdown 출력
   - 한 줄 요약 (grep 가능)

## Scope — DO / DO NOT

### DO (범위 안)
- 컴파일 SQL 컬럼 변경 감지
- materialization × on_schema_change 규칙 기반 위험도 판정
- 하위 모델 cascade 영향 분석 (broken ref, build failure)
- materialization/on_schema_change 설정 변경 감지

### DO NOT (범위 밖)
- `dbt run` 시뮬레이션 — 런타임 동작은 범위 밖
- Warehouse 접속 — 순수 파일 분석만
- `full_refresh` 모드 판정 — 런타임 플래그는 CI 환경에서 결정
- `seed`/`source` 변경 감지 — 컴파일 SQL 기반 도구
- `pre_hook`/`post_hook` DDL 분석 — 복잡성 대비 가치 낮음

## Architecture

```
src/dbt_plan/
├── columns.py      # SQLGlot 기반 컬럼 추출 (multi-dialect)
├── config.py       # .dbt-plan.yml + env var 설정
├── predictor.py    # DDL 예측 규칙 + cascade 분석
├── manifest.py     # manifest.json 파싱, node index, downstream BFS
├── diff.py         # compiled SQL 디렉토리 비교 (캐싱)
├── formatter.py    # text (color) / github / json 출력
└── cli.py          # CLI: snapshot, check, init, stats, run, ci-setup
```

## Development

```bash
uv sync --extra test
make test
```

## Rules

- sqlglot 외 런타임 의존성 추가 금지
- 파싱 실패 시 safe 반환 절대 금지 — None 반환 → 호출자가 review로 처리
- 테스트 없는 기능 추가 금지 (TDD)
- SELECT \* → ["*"] 반환 (manifest column fallback 지원)
- Multi-dialect 지원 via --dialect (기본값: snowflake)
- `enabled: false` 모델은 인덱싱에서 제외

## DDL Prediction Rules

| materialization | on_schema_change | DDL | Safety |
|---|---|---|---|
| table | * | CREATE OR REPLACE TABLE | SAFE |
| view | * | CREATE OR REPLACE VIEW | SAFE |
| ephemeral | * | (no physical object) | SAFE |
| snapshot | * | REVIEW REQUIRED | WARNING |
| incremental | ignore | no DDL | SAFE |
| incremental | fail | build failure | WARNING |
| incremental | append_new_columns | ADD COLUMN only | SAFE |
| incremental | sync_all_columns | ADD + DROP COLUMN | DESTRUCTIVE if removed |
| any | (model removed) | MODEL REMOVED | DESTRUCTIVE |
| any | (unknown osc) | UNKNOWN on_schema_change | WARNING |

## Testing

```bash
make test                    # all tests
make test-cov                # with coverage
make lint                    # ruff check
pytest tests/test_columns.py # specific module
```

Test fixtures in `tests/fixtures/` contain real-world compiled SQL patterns.
