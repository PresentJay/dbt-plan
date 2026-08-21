# Sample Project

dbt-plan을 체험해볼 수 있는 예제 프로젝트입니다. Snowflake 접속 없이 로컬에서 바로 실행됩니다.

## 시나리오

| 모델 | 변경 | 예상 결과 |
|------|------|-----------|
| `int_order_enriched` | `shipping_info`, `billing_info` 삭제, `shipping_city`, `billing_method` 추가 | **DESTRUCTIVE** (sync_all_columns + DROP COLUMN) |
| `fct_daily_sales` | `total_sales` 컬럼 추가, `shipping_info` 참조 유지 | **SAFE** (append_new_columns) + **BROKEN_REF** cascade |
| `dim_customers` | `platform` 컬럼 추가 | **SAFE** (table = CREATE OR REPLACE) |
| `dim_publishers` | 새 모델 | **SAFE** (신규 테이블) |

## 실행

```bash
pip install dbtplan  # or: pip install git+https://github.com/PresentJay/dbt-plan
cd examples/sample-project
bash run-example.sh
```

## 기대 출력

```text
dbt-plan -- 4 model(s) changed

DESTRUCTIVE  int_order_enriched (incremental, sync_all_columns)
  ADD COLUMN  shipping_city
  ADD COLUMN  billing_method
  DROP COLUMN  shipping_info
  DROP COLUMN  billing_info
  Downstream: dim_customers, fct_daily_sales (2 model(s))
  >> BROKEN_REF  fct_daily_sales: references dropped column(s): shipping_info

SAFE  dim_publishers (table)
  CREATE OR REPLACE TABLE

SAFE  dim_customers (table)
  CREATE OR REPLACE TABLE

SAFE  fct_daily_sales (incremental, append_new_columns)
  ADD COLUMN  total_sales

dbt-plan: 4 checked, 3 safe, 0 warning, 1 destructive, 1 cascade risk(s)
```

Exit code: **1** (destructive — int_order_enriched에 DROP COLUMN + fct_daily_sales cascade broken ref)

## 구조

```text
base/                              # snapshot (변경 전)
├── compiled/*.sql
└── manifest.json

current/                           # 현재 상태 (변경 후)
└── target/
    ├── compiled/sample/models/*.sql
    └── manifest.json
```
