-- Pattern: DuckDB ASOF JOIN projection
-- Expected: ["order_id", "unit_price"]
SELECT o.order_id, p.price AS unit_price FROM orders AS o ASOF LEFT JOIN book_prices AS p ON o.book_id = p.book_id AND o.ordered_at >= p.valid_from;