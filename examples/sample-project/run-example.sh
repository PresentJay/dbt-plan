#!/bin/bash
# dbt-plan 체험용 예제
# Snowflake 접속 없이 로컬에서 바로 실행할 수 있습니다.
#
# 사용법:
#   pip install dbt-plan  # or: pip install git+https://github.com/PresentJay/dbt-plan
#   cd examples/sample-project
#   bash run-example.sh
#
# Requires dbt-plan >= 0.16.0: codes 0/1/2 are completed findings; 3 is an error.
# Legacy 0.15.x also uses 1/2 for errors; see ../../docs/exit-codes.md.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

run_check() {
  EXIT_CODE=0
  dbt-plan check \
    --base-dir "$SCRIPT_DIR/base" \
    --project-dir "$SCRIPT_DIR/current" \
    "$@" || EXIT_CODE=$?
  case "$EXIT_CODE" in
    0|1|2) ;;
    *) echo "dbt-plan execution failed (exit code: $EXIT_CODE)." >&2; exit 3 ;;
  esac
}

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
run_check --format text

echo ""
echo "--- GitHub markdown output ---"
echo ""
run_check --format github

echo ""
echo "--- JSON output (cascade risks in summary) ---"
echo ""
run_check --format json

echo ""
echo "--- Exit code check ---"
run_check > /dev/null
echo "exit code: $EXIT_CODE (0=safe, 1=destructive, 2=warning, 3=execution error)"
echo ""
echo "이 예제에서는 int_order_enriched에 DROP COLUMN + fct_daily_sales cascade broken ref이 있으므로 exit 1 (destructive) 입니다."
