#!/bin/bash
# dbt-plan 체험용 예제
# Snowflake 접속 없이 로컬에서 바로 실행할 수 있습니다.
#
# 사용법:
#   pip install dbtplan  # or: pip install git+https://github.com/PresentJay/dbt-plan
#   cd examples/sample-project
#   bash run-example.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== dbt-plan example ==="
echo ""
echo "시나리오:"
echo "  - int_order_enriched: shipping_info, billing_info 삭제 → shipping_city, billing_method 추가 (DESTRUCTIVE)"
echo "  - fct_daily_sales: total_sales 추가, shipping_info 참조 유지 → BROKEN_REF cascade"
echo "  - dim_customers: customer_tier 컬럼 추가 (SAFE, table = CREATE OR REPLACE)"
echo "  - dim_publishers: 새 모델 추가 (SAFE)"
echo ""

echo "--- Text output ---"
echo ""
dbt-plan check \
  --base-dir "$SCRIPT_DIR/base" \
  --project-dir "$SCRIPT_DIR/current" \
  --format text || true

echo ""
echo "--- GitHub markdown output ---"
echo ""
dbt-plan check \
  --base-dir "$SCRIPT_DIR/base" \
  --project-dir "$SCRIPT_DIR/current" \
  --format github || true

echo ""
echo "--- JSON output (cascade risks in summary) ---"
echo ""
dbt-plan check \
  --base-dir "$SCRIPT_DIR/base" \
  --project-dir "$SCRIPT_DIR/current" \
  --format json || true

echo ""
echo "--- Exit code check ---"
set +e
dbt-plan check \
  --base-dir "$SCRIPT_DIR/base" \
  --project-dir "$SCRIPT_DIR/current" \
  > /dev/null 2>&1
EXIT_CODE=$?
set -e
echo "exit code: $EXIT_CODE (0=safe, 1=destructive, 2=error)"
echo ""
echo "이 예제에서는 int_order_enriched에 DROP COLUMN + fct_daily_sales cascade broken ref이 있으므로 exit 1 (destructive) 입니다."
