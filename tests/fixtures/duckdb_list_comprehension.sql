-- Pattern: DuckDB list-comprehension projection
-- Expected: ["order_id", "doubled_quantities"]
SELECT
    order_id,
    [x * 2 FOR x IN quantities IF x > 0] AS doubled_quantities
FROM orders;