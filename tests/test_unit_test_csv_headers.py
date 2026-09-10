from dbt_plan.manifest import _fixture_columns

def test_unquoted_and_quoted_headers_are_equivalent():
    # Positive control: standard CSV
    cols, reason = _fixture_columns({"format": "csv", "rows": "order_id,customer_id\n1,2"})
    assert cols == frozenset({"order_id", "customer_id"})
    assert not reason
    
    # Quoted equivalent
    cols, reason = _fixture_columns({"format": "csv", "rows": '"order_id","customer_id"\n1,2'})
    assert cols == frozenset({"order_id", "customer_id"})
    assert not reason

def test_escaped_quotes_and_commas():
    # Quotes inside quotes
    cols, reason = _fixture_columns({"format": "csv", "rows": '"order_""id","cust,omer_id"\n1,2'})
    assert cols == frozenset({'order_"id', 'cust,omer_id'})
    assert not reason

def test_crlf_and_bom():
    cols, reason = _fixture_columns({"format": "csv", "rows": '\ufefforder_id,customer_id\r\n1,2'})
    assert cols == frozenset({"order_id", "customer_id"})
    assert not reason

def test_unterminated_quote_is_malformed():
    # Negative control: malformed CSV
    cols, reason = _fixture_columns({"format": "csv", "rows": '"order_id,customer_id\n1,2'})
    assert cols is None
    assert "malformed" in reason or "CSV" in reason

def test_empty_csv():
    # Empty string
    cols, reason = _fixture_columns({"format": "csv", "rows": ""})
    assert cols is None
    assert "no inline header" in reason

def test_empty_header():
    # Only newline
    cols, reason = _fixture_columns({"format": "csv", "rows": "\n\n"})
    assert cols is None
    assert "no inline header" in reason

def test_blank_column_name():
    # Blank header
    cols, reason = _fixture_columns({"format": "csv", "rows": "order_id,,customer_id\n1,2,3"})
    assert cols is None
    assert "blank column name" in reason

def test_duplicate_headers():
    # Duplicate headers
    cols, reason = _fixture_columns({"format": "csv", "rows": "order_id,ORDER_ID\n1,2"})
    assert cols is None
    assert "duplicate" in reason
